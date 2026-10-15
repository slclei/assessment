import json
import difflib
import io
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Optional
from contextlib import closing
from datetime import datetime, timezone

from src.local_agents import LocalTaskPlannerAgent, RequirementAnalystAgent, SDLCArtifactAgent
from src.guardrails import PolicyGuardrailEngine
from src.orchestrator import OrchestratorEngine
from src.run_manifest import write_run_manifest
from src.agent_provider import AgentProvider
from src.models import TaskOutputRevision
from src.url_shortener import create_app


DEFAULT_PROMPT = "Build a resilient URL shortener with redirects, click analytics, and custom aliases."


def _run_replanning_demonstration(output_dir):
    report_path = output_dir / "replanning_demo.json"
    observations = []
    compensations = []

    with tempfile.TemporaryDirectory() as working_directory:
        implementation_path = Path(working_directory) / "implementation.json"
        verification_path = Path(working_directory) / "verification.json"
        release_path = Path(working_directory) / "release.json"
        engine = OrchestratorEngine(scenario_name="Ambiguous-Upstream-Revision")

        def write_implementation(context, expiration_hours):
            policy = {"default_expiration_hours": expiration_hours}
            implementation_path.write_text(json.dumps(policy), encoding="utf-8")
            context["default_expiration_hours"] = expiration_hours
            return policy

        design_task = engine.add_task(
            "Approve initial expiry policy",
            lambda context: context.update({"default_expiration_hours": 24}) or {"default_expiration_hours": 24},
        )
        implementation_task = engine.add_task(
            "Implement expiry policy",
            lambda context: write_implementation(context, context["default_expiration_hours"]),
            dependencies=[design_task],
            compensate_action=lambda context: implementation_path.unlink(missing_ok=True),
        )

        def verify_implementation(context):
            policy = json.loads(implementation_path.read_text(encoding="utf-8"))
            expiration_hours = policy["default_expiration_hours"]
            if expiration_hours != context["default_expiration_hours"]:
                raise ValueError("Verification observed stale expiry policy output.")

            database_path = Path(working_directory) / f"verification-{expiration_hours}.db"
            application = create_app(
                str(database_path),
                enabled_capabilities=("shorten_urls", "redirect_urls", "expiring_links"),
                default_expiration_hours=expiration_hours,
                max_expiration_hours=expiration_hours,
            )
            body = json.dumps({"long_url": "https://example.com/replanning"}).encode("utf-8")
            response = {}
            response_body = b"".join(application({
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/shorten",
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": io.BytesIO(body),
            }, lambda status, headers: response.update(status=status)))
            if not response.get("status", "").startswith("201"):
                raise ValueError("Revised service did not create an expiring URL.")
            short_code = json.loads(response_body)["short_code"]
            with closing(sqlite3.connect(database_path)) as connection:
                expires_at = connection.execute(
                    "SELECT expires_at FROM urls WHERE short_code = ?", (short_code,)
                ).fetchone()[0]
            remaining_hours = (
                datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)
            ).total_seconds() / 3600
            if not expiration_hours - 0.01 <= remaining_hours <= expiration_hours:
                raise ValueError("Service expiration did not match the approved policy.")
            observations.append(expiration_hours)
            verification_path.write_text(json.dumps({"passed": True, **policy}), encoding="utf-8")
            return {"passed": True, **policy}

        verification_task = engine.add_task(
            "Verify expiry policy",
            verify_implementation,
            dependencies=[implementation_task],
            compensate_action=lambda context: compensations.append("verification") or verification_path.unlink(missing_ok=True),
        )

        def verify_release(context):
            verification = json.loads(verification_path.read_text(encoding="utf-8"))
            if not verification.get("passed"):
                raise ValueError("Release requires passing verification.")
            release = {"passed": True, "default_expiration_hours": verification["default_expiration_hours"]}
            release_path.write_text(json.dumps(release), encoding="utf-8")
            return release

        release_task = engine.add_task(
            "Prepare release from verified policy",
            verify_release,
            dependencies=[verification_task],
            compensate_action=lambda context: compensations.append("release") or release_path.unlink(missing_ok=True),
        )

        def revise_policy(context):
            revised_policy = write_implementation(context, 168)
            return TaskOutputRevision(
                implementation_task,
                revised_policy,
                "Stakeholder changed the default link lifetime from 24 hours to seven days.",
            )

        engine.add_task(
            "Revise upstream expiry requirement",
            revise_policy,
            dependencies=[release_task],
            requires_approval=True,
            approval_details="Review the seven-day lifetime change. Stale verification and release artifacts will be compensated and rerun.",
            may_revise_output=True,
        )
        success = engine.execute()

    events = [entry["event"] for entry in engine.audit_lineage]
    report = {
        "run_id": engine.run_id,
        "passed": success and observations == [24, 168],
        "initial_expiration_hours": 24,
        "revised_expiration_hours": 168,
        "verified_expiry_values": observations,
        "compensation_order": compensations,
        "revision_events": [
            event for event in events
            if event in {"OUTPUT_REVISED", "REPLAN_INVALIDATED", "REPLAN_COMPENSATED"}
        ],
        "approvals": [
            {"event": item["event"], "details": item["details"]}
            for item in engine.audit_lineage
            if item["event"] in {"PENDING_APPROVAL", "APPROVED"}
        ],
        "release_after_revision": json.loads(release_path.read_text(encoding="utf-8")) if release_path.exists() else None,
    }
    PolicyGuardrailEngine.write_artifact(
        str(report_path), json.dumps(report, indent=2), str(output_dir)
    )
    return report


def run_ambiguous_scenario(prompt: Optional[str] = None, provider: Optional[AgentProvider] = None):
    request = prompt or DEFAULT_PROMPT
    analyst = RequirementAnalystAgent(provider)
    planner = LocalTaskPlannerAgent(provider)
    artifact_agent = SDLCArtifactAgent(provider)
    analysis = analyst.analyze(request)

    engine = OrchestratorEngine(scenario_name="Ambiguous-Intent-Decomposition")
    output_dir = Path("output_artifacts").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    clarification_path = output_dir / "intent_clarification.json"
    graph_path = output_dir / "task_breakdown.json"
    approval_path = output_dir / "stakeholder_approval.json"
    final_validation_path = output_dir / "ambiguous_final_validation.json"
    unsupported_path = output_dir / "unsupported_requirements.json"

    def write_json(path, content):
        return PolicyGuardrailEngine.write_artifact(
            str(path), json.dumps(content, indent=2), str(output_dir)
        )

    scenario_artifacts = [
        clarification_path,
        graph_path,
        approval_path,
        final_validation_path,
        unsupported_path,
        output_dir / "ambiguous_run_manifest.json",
        output_dir / "architecture.md",
        output_dir / "generated_app.py",
        output_dir / "implementation.diff",
        output_dir / "replanning_demo.json",
        output_dir / "acceptance_test_report.json",
        output_dir / "setup_and_operations.md",
        output_dir / "release_readiness.json",
    ]
    for path in scenario_artifacts:
        path.unlink(missing_ok=True)

    if analysis["unsupported_requirements"]:
        write_json(clarification_path, analysis)
        write_json(unsupported_path, {
            "blocked": True,
            "reason": "The local implementation cannot safely claim completion for these capabilities.",
            "requirements": analysis["unsupported_requirements"],
        })
        engine.context["analysis"] = analysis
        engine.add_task(
            "Block Unsupported Requirements",
            action=lambda ctx: None,
            policy_check=lambda ctx: False,
            max_retries=1,
        )
        success = engine.execute()
        write_run_manifest(
            output_dir,
            engine,
            request,
            [item["id"] for item in analysis["requirements"]],
            [path.name for path in scenario_artifacts if path.name != "ambiguous_run_manifest.json"],
            manifest_name="ambiguous_run_manifest.json",
            provider=provider,
        )
        return engine, success

    plan = planner.plan(analysis)
    scenario_artifacts.extend(
        output_dir / node["artifact"] for node in plan["nodes"] if node["role"] == "testing"
    )

    def cleanup_artifacts(ctx):
        for path in output_dir.iterdir():
            if path.is_file() and (
                path.name == "intent_clarification.json"
                or path.name == "task_breakdown.json"
                or path.name == "stakeholder_approval.json"
                or path.name == "ambiguous_final_validation.json"
                or path.name == "unsupported_requirements.json"
                or path.name in {"implementation.diff", "replanning_demo.json"}
                or path.name in {node["artifact"] for node in plan["nodes"]}
            ):
                path.unlink(missing_ok=True)

    def record_analysis(ctx):
        write_json(clarification_path, analysis)
        runtime_analysis = dict(analysis)
        runtime_analysis["output_dir"] = str(output_dir)
        ctx["analysis"] = runtime_analysis
        return {
            "requirement_count": len(analysis["requirements"]),
            "unresolved_question_count": len(analysis["unresolved_questions"]),
            "assumption_count": len(analysis["assumptions"]),
        }

    def record_plan(ctx):
        if not planner.validate(plan):
            raise ValueError("Generated plan failed dependency or requirement coverage checks.")
        write_json(graph_path, plan)
        ctx["task_graph"] = plan
        return {"task_count": len(plan["nodes"]), "entry_nodes": plan["entry_nodes"], "exit_nodes": plan["exit_nodes"]}

    def create_role_action(node):
        def run_agent(ctx):
            current_plan = ctx["task_graph"]
            current_node = next(item for item in current_plan["nodes"] if item["id"] == node["id"])
            runtime_analysis = ctx["analysis"]
            if current_node["role"] == "release":
                reports = [
                    json.loads((output_dir / node["artifact"]).read_text(encoding="utf-8"))
                    for node in current_plan["nodes"]
                    if node["role"] == "testing"
                ]
                hashes = {report.get("implementation_sha256") for report in reports}
                acceptance_result = {
                    "passed": bool(reports) and all(report.get("passed") for report in reports),
                    "checks": [check for report in reports for check in report.get("checks", [])],
                    "validated_requirement_ids": sorted({
                        requirement_id
                        for report in reports
                        for requirement_id in report.get("validated_requirement_ids", [])
                    }),
                    "implementation_sha256": next(iter(hashes)) if len(hashes) == 1 else None,
                    "requirement_reports": [
                        node["artifact"] for node in current_plan["nodes"]
                        if node["role"] == "testing"
                    ],
                }
                write_json(output_dir / "acceptance_test_report.json", acceptance_result)
                runtime_analysis = dict(runtime_analysis)
                runtime_analysis["acceptance_test_result"] = acceptance_result
            content = artifact_agent.render(current_node, runtime_analysis)
            if current_node["role"] == "release":
                required_artifacts = [
                    output_dir / item["artifact"]
                    for item in current_plan["nodes"]
                    if item["role"] != "release"
                ]
                required_artifacts.append(output_dir / "implementation.diff")
                if not planner.validate(current_plan) or not all(path.is_file() and path.stat().st_size for path in required_artifacts):
                    raise ValueError("Release gate failed: plan or lifecycle artifacts are incomplete.")
                content["validated_artifacts"] = [path.name for path in required_artifacts]
                diff_path = output_dir / "implementation.diff"
                diff_content = diff_path.read_text(encoding="utf-8") if diff_path.is_file() else ""
                content["review_diff"] = {
                    "path": diff_path.name,
                    "preview": diff_content[:3000],
                    "truncated": len(diff_content) > 3000,
                }
            if current_node["role"] == "implementation":
                maintained_source = (Path(__file__).resolve().parents[1] / "src" / "url_shortener.py").read_text(encoding="utf-8")
                artifact_source = content if isinstance(content, str) else json.dumps(content, indent=2)
                diff = difflib.unified_diff(
                    maintained_source.splitlines(keepends=True),
                    artifact_source.splitlines(keepends=True),
                    fromfile="src/url_shortener.py",
                    tofile="generated_app.py",
                )
                PolicyGuardrailEngine.write_artifact(
                    str(output_dir / "implementation.diff"), "".join(diff), str(output_dir)
                )
            artifact_path = artifact_agent.write_artifact(output_dir, current_node, content)
            result = {"role": current_node["role"], "artifact": artifact_path.name}
            if current_node["role"] == "release":
                result["review_diff"] = content["review_diff"]
            return result
        return run_agent

    def record_stakeholder_approval(ctx):
        approval = {
            "status": "approved",
            "approved_at": time.time(),
            "release_artifact": "output_artifacts/release_readiness.json",
        }
        write_json(approval_path, approval)
        return approval

    def validate_final_artifacts(ctx):
        required_paths = [clarification_path, graph_path, approval_path, output_dir / "acceptance_test_report.json"]
        required_paths.extend(output_dir / node["artifact"] for node in plan["nodes"])
        if not all(path.is_file() and path.stat().st_size for path in required_paths):
            raise ValueError("One or more planned SDLC artifacts are missing or empty.")
        release_report = json.loads((output_dir / "release_readiness.json").read_text(encoding="utf-8"))
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        if not release_report.get("passed") or approval.get("status") != "approved":
            raise ValueError("Release readiness or stakeholder approval is incomplete.")
        report = {
            "passed": True,
            "prompt": request,
            "requirement_count": len(analysis["requirements"]),
            "validated_artifacts": [path.name for path in required_paths[:-1]],
        }
        write_json(final_validation_path, report)
        return report

    analysis_task = engine.add_task(
        "Analyze User Requirements",
        record_analysis,
        compensate_action=cleanup_artifacts,
    )
    plan_task = engine.add_task("Plan SDLC Task Graph", record_plan, dependencies=[analysis_task])

    task_ids = {}
    for node in plan["nodes"]:
        dependencies = [plan_task]
        dependencies.extend(task_ids[dependency] for dependency in node["depends_on"])
        task_ids[node["id"]] = engine.add_task(
            f"{node['role'].title()} Agent: {node['id']}",
            create_role_action(node),
            dependencies=dependencies,
        )

    release_task = task_ids[plan["exit_nodes"][0]]
    approval_task = engine.add_task(
        "Stakeholder Release Approval",
        record_stakeholder_approval,
        dependencies=[release_task],
        requires_approval=True,
        approval_details="Review implementation.diff and the release report; approve only when all planned requirement tests pass.",
    )
    engine.add_task("Validate Final SDLC Artifacts", validate_final_artifacts, dependencies=[approval_task])

    success = engine.execute()
    replanning_report = _run_replanning_demonstration(output_dir) if success else {
        "passed": False,
        "skipped": True,
        "reason": "Primary workflow did not pass its release gate.",
    }
    write_run_manifest(
        output_dir,
        engine,
        request,
        [item["id"] for item in analysis["requirements"]],
        [path.name for path in scenario_artifacts if path.name != "ambiguous_run_manifest.json"],
        inputs={"replanning_demo": replanning_report},
        manifest_name="ambiguous_run_manifest.json",
        provider=provider,
    )
    return engine, success and replanning_report["passed"] and final_validation_path.is_file()
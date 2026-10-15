import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from src.agent_provider import AgentProvider
from src.guardrails import PolicyGuardrailEngine


_CAPABILITIES = (
    ("shorten_urls", (r"url shortener", r"shorten\w*", r"short url", r"short link"), "Create unique short codes for valid URLs."),
    ("redirect_urls", (r"redirect\w*", r"resolve short", r"destination url"), "Resolve known short codes to their destination URLs."),
    ("click_analytics", (r"analytics", r"click count", r"track clicks", r"usage metrics"), "Record and report aggregate link usage."),
    ("custom_aliases", (r"custom alias(?:es)?", r"custom code", r"vanity url", r"user-defined alias"), "Support user-selected short codes subject to collision checks."),
    ("security_controls", (r"security", r"secure", r"authentication", r"authorization"), "Apply the requested security and access controls."),
    ("reliability_controls", (r"resilien\w*", r"reliab\w*", r"availability", r"fault tolerant"), "Use bounded short-code collision retries and request-size limits; no high-availability guarantee is claimed."),
    ("expiring_links", (r"expir\w*", r"lifetime"), "Apply the normalized default link lifetime, allow shorter timezone-aware expirations, and reject redirects after expiry."),
)
_SUPPORTED_CAPABILITIES = {
    "shorten_urls",
    "redirect_urls",
    "click_analytics",
    "custom_aliases",
    "reliability_controls",
    "expiring_links",
}
_PROMPT_FILLER_WORDS = {
    "a", "add", "also", "an", "and", "app", "application", "as", "build", "can",
    "click", "create", "for", "from", "include", "including", "implement", "it", "link",
    "links", "make", "my", "new", "of", "our", "please", "prototype", "service",
    "shortener", "shorteners", "support", "system", "that", "the", "them", "this", "to", "url",
    "after", "allow", "day", "days", "default", "hour", "hours", "in", "lifetime", "minute", "minutes", "seven", "set", "urls", "want", "we", "with", "you",
}
_DURATION_UNITS_IN_HOURS = {"hour": 1, "day": 24, "week": 168}
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _normalized_expiration_constraints(prompt):
    number = r"(?P<count>\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
    unit = r"(?P<unit>hours?|days?|weeks?)"
    duration = rf"{number}\s*[- ]?{unit}"
    default_lifetime = r"(?:default(?:\s+link)?\s+(?:lifetime|expir\w*)|(?:lifetime|expir\w*)\s+default)"
    patterns = (
        rf"\b{default_lifetime}\s*(?:is|of|to|at|:)?\s*{duration}\b",
        rf"\b{duration}\s+default(?:\s+link)?\s+(?:lifetime|expir\w*)\b",
    )
    matches = [re.search(pattern, prompt.casefold()) for pattern in patterns]
    matches = [match for match in matches if match]
    if not matches:
        return {"default_expiration_hours": 24, "max_expiration_hours": 24}
    values = set()
    for match in matches:
        count_token = match.group("count")
        count = int(count_token) if count_token.isdigit() else _NUMBER_WORDS[count_token]
        unit_token = match.group("unit").rstrip("s")
        hours = count * _DURATION_UNITS_IN_HOURS[unit_token]
        if not 1 <= hours <= 8760:
            raise ValueError("Requested default link lifetime must be from 1 to 8760 hours.")
        values.add(hours)
    if len(values) != 1:
        raise ValueError("Conflicting default link lifetimes were requested.")
    hours = values.pop()
    return {"default_expiration_hours": hours, "max_expiration_hours": hours}


def _apply_expiration_constraints(prompt, requirements):
    constraints = _normalized_expiration_constraints(prompt)
    if constraints["default_expiration_hours"] != 24 or re.search(r"lifetime|expir\w*", prompt, re.IGNORECASE):
        if "expiring_links" not in {item["id"] for item in requirements}:
            requirements.append({
                "id": "expiring_links",
                "description": "Apply the explicitly requested default link lifetime, allow shorter timezone-aware expirations, and reject redirects after expiry.",
                "supported": True,
            })
    return constraints


def _normalized_expiration_assumptions(assumptions, requirements, constraints):
    if "expiring_links" not in {item["id"] for item in requirements}:
        return assumptions
    assumptions = [
        item for item in assumptions
        if not re.search(r"expir|expiry|lifetime", item, re.IGNORECASE)
    ]
    assumptions.append(
        f"Use a {constraints['default_expiration_hours']}-hour default lifetime and "
        f"permit timezone-aware explicit expirations up to {constraints['max_expiration_hours']} hours."
    )
    return assumptions


class RequirementAnalystAgent:
    """Deterministic local analyst that extracts capabilities and exposes ambiguity."""

    def __init__(self, provider: AgentProvider | None = None):
        self.provider = provider

    def analyze(self, prompt):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("A non-empty software requirement prompt is required.")
        if len(prompt) > 4000:
            raise ValueError("Requirement prompt must be 4000 characters or fewer.")

        normalized_prompt = " ".join(prompt.split())
        if self.provider is not None:
            return self._analyze_with_provider(normalized_prompt)
        lowered_prompt = normalized_prompt.casefold()
        requirements = [
            {
                "id": requirement_id,
                "description": description,
                "supported": requirement_id in _SUPPORTED_CAPABILITIES,
            }
            for requirement_id, patterns, description in _CAPABILITIES
            if any(re.search(pattern, lowered_prompt) for pattern in patterns)
        ]
        constraints = _apply_expiration_constraints(normalized_prompt, requirements)
        recognized_prompt = lowered_prompt
        for _, patterns, _ in _CAPABILITIES:
            for pattern in patterns:
                recognized_prompt = re.sub(pattern, " ", recognized_prompt)
        recognized_prompt = re.sub(r"\b\d+\b", " ", recognized_prompt)
        unrecognized_terms = sorted({
            token for token in re.findall(r"[a-z0-9]+", recognized_prompt)
            if token not in _PROMPT_FILLER_WORDS
        })
        if requirements and any(item["supported"] for item in requirements) and unrecognized_terms:
            requirements.append({
                "id": "unsupported_scope",
                "description": "Unrecognized capability terms: " + ", ".join(unrecognized_terms),
                "supported": False,
            })
        if not requirements:
            requirements.append({
                "id": "unsupported_request",
                "description": normalized_prompt,
                "supported": False,
            })

        requirement_ids = {item["id"] for item in requirements}
        if requirement_ids.intersection(_SUPPORTED_CAPABILITIES):
            for required_id in ("shorten_urls", "redirect_urls"):
                if required_id not in requirement_ids:
                    requirements.append(next(
                        {
                            "id": requirement_id,
                            "description": description,
                            "supported": requirement_id in _SUPPORTED_CAPABILITIES,
                        }
                        for requirement_id, _, description in _CAPABILITIES
                        if requirement_id == required_id
                    ))
                    requirement_ids.add(required_id)

        unsupported_requirements = [item for item in requirements if not item["supported"]]

        unresolved_questions = []
        assumptions = []
        if "click_analytics" in {item["id"] for item in requirements}:
            unresolved_questions.append("What retention period should apply to analytics data?")
            assumptions.append("Collect aggregate counts only until a retention policy is approved.")
        if "custom_aliases" in {item["id"] for item in requirements}:
            unresolved_questions.append("Are aliases globally unique or scoped to individual users?")
            assumptions.append("Require globally unique aliases for the initial design.")
        if "security_controls" in {item["id"] for item in requirements}:
            unresolved_questions.append("Which identity provider and authorization roles are required?")
            assumptions.append("Keep authentication provider selection open pending stakeholder input.")
        if "reliability_controls" in {item["id"] for item in requirements}:
            unresolved_questions.append("What availability target and recovery objective should the service meet?")
            assumptions.append("Validate local failure controls only; do not claim high availability until an SLO and deployment design are approved.")
        if "expiring_links" in {item["id"] for item in requirements}:
            assumptions.append("New links use the normalized default lifetime unless a shorter timezone-aware ISO-8601 expires_at is supplied.")
        if not unresolved_questions:
            unresolved_questions.append("What scale and service-level target should the first release support?")
            assumptions.append("Use a single-instance prototype target until scale requirements are clarified.")
        assumptions = _normalized_expiration_assumptions(assumptions, requirements, constraints)

        return {
            "raw_prompt": normalized_prompt,
            "requirements": requirements,
            "constraints": constraints,
            "unsupported_requirements": unsupported_requirements,
            "unresolved_questions": unresolved_questions,
            "assumptions": assumptions,
        }

    def _analyze_with_provider(self, prompt):
        response = self.provider.complete(
            "analyst",
            "Interpret the software request using only the listed capability IDs. "
            "Treat normalized constraints as explicit user requirements, not unsupported capabilities. "
            "Return one JSON object with requirement_ids (array of IDs), unsupported_requirements "
            "(array of strings), unresolved_questions (array of strings), and assumptions (array of strings). "
            "Never claim an unlisted capability is supported.",
            {
                "prompt": prompt,
                "normalized_constraints": _normalized_expiration_constraints(prompt),
                "capabilities": [
                    {"id": item[0], "description": item[2], "supported": item[0] in _SUPPORTED_CAPABILITIES}
                    for item in _CAPABILITIES
                ],
            },
        )
        try:
            result = json.loads(response)
        except json.JSONDecodeError as error:
            raise ValueError("Analyst provider must return valid JSON.") from error
        if not isinstance(result, dict):
            raise ValueError("Analyst provider response must be a JSON object.")
        requirement_ids = result.get("requirement_ids", [])
        unsupported = result.get("unsupported_requirements", [])
        questions = result.get("unresolved_questions", [])
        assumptions = result.get("assumptions", [])
        if (
            not isinstance(requirement_ids, list)
            or not all(isinstance(item, str) for item in requirement_ids)
            or not isinstance(unsupported, list)
            or not all(isinstance(item, str) for item in unsupported)
            or not isinstance(questions, list)
            or not all(isinstance(item, str) for item in questions)
            or not isinstance(assumptions, list)
            or not all(isinstance(item, str) for item in assumptions)
        ):
            raise ValueError("Analyst provider returned fields with invalid types.")

        descriptions = {item[0]: item[2] for item in _CAPABILITIES}
        selected_ids = list(dict.fromkeys(requirement_ids))
        requirements = [
            {
                "id": requirement_id,
                "description": descriptions.get(requirement_id, "Provider identified an unsupported capability."),
                "supported": requirement_id in _SUPPORTED_CAPABILITIES,
            }
            for requirement_id in selected_ids
        ]
        constraints = _apply_expiration_constraints(prompt, requirements)
        unknown_ids = [item for item in selected_ids if item not in descriptions]
        if unknown_ids:
            unsupported.extend(f"Unsupported capability ID: {item}" for item in unknown_ids)
        if unsupported:
            requirements.append({
                "id": "unsupported_scope",
                "description": "; ".join(unsupported),
                "supported": False,
            })
        if not requirements:
            requirements.append({
                "id": "unsupported_request",
                "description": prompt,
                "supported": False,
            })

        requirement_ids = {item["id"] for item in requirements}
        if requirement_ids.intersection(_SUPPORTED_CAPABILITIES):
            for required_id in ("shorten_urls", "redirect_urls"):
                if required_id not in requirement_ids:
                    requirements.append({
                        "id": required_id,
                        "description": descriptions[required_id],
                        "supported": True,
                    })
                    requirement_ids.add(required_id)
        recognized_prompt = prompt.casefold()
        for _, patterns, _ in _CAPABILITIES:
            for pattern in patterns:
                recognized_prompt = re.sub(pattern, " ", recognized_prompt)
        recognized_prompt = re.sub(r"\b\d+\b", " ", recognized_prompt)
        unrecognized_terms = sorted({
            token for token in re.findall(r"[a-z0-9]+", recognized_prompt)
            if token not in _PROMPT_FILLER_WORDS
        })
        if unrecognized_terms and any(item["supported"] for item in requirements):
            requirements.append({
                "id": "unsupported_scope",
                "description": "Unrecognized capability terms: " + ", ".join(unrecognized_terms),
                "supported": False,
            })
        if not questions:
            questions = ["What scale and service-level target should the first release support?"]
        if not assumptions:
            assumptions = ["Use a single-instance prototype target until scale requirements are clarified."]
        assumptions = _normalized_expiration_assumptions(assumptions, requirements, constraints)
        return {
            "raw_prompt": prompt,
            "requirements": requirements,
            "constraints": constraints,
            "unsupported_requirements": [item for item in requirements if not item["supported"]],
            "unresolved_questions": questions,
            "assumptions": assumptions,
        }


class LocalTaskPlannerAgent:
    """Builds an acyclic implementation/test/documentation/release graph from requirements."""

    def __init__(self, provider: AgentProvider | None = None):
        self.provider = provider

    def plan(self, analysis):
        requirements = analysis.get("requirements", [])
        if analysis.get("unsupported_requirements"):
            unsupported_ids = [item["id"] for item in analysis["unsupported_requirements"]]
            raise ValueError(f"Cannot plan unsupported requirements: {', '.join(unsupported_ids)}")
        requirement_ids = [item["id"] for item in requirements]
        if not requirement_ids:
            raise ValueError("Cannot plan a workflow without normalized requirements.")

        implementation_node = {
            "id": "implementation",
            "role": "implementation",
            "requirement_ids": requirement_ids,
            "depends_on": ["architecture"],
            "artifact": "generated_app.py",
        }
        verification_nodes = [
            {
                "id": f"verification_{requirement_id}",
                "role": "testing",
                "requirement_ids": [requirement_id],
                "depends_on": ["implementation"],
                "artifact": f"acceptance_test_report_{requirement_id}.json",
            }
            for requirement_id in requirement_ids
        ]
        verification_ids = [node["id"] for node in verification_nodes]
        nodes = [
            {
                "id": "architecture",
                "role": "architecture",
                "requirement_ids": requirement_ids,
                "depends_on": [],
                "artifact": "architecture.md",
            },
            implementation_node,
            *verification_nodes,
            {
                "id": "documentation",
                "role": "documentation",
                "requirement_ids": requirement_ids,
                "depends_on": ["architecture", "implementation", *verification_ids],
                "artifact": "setup_and_operations.md",
            },
            {
                "id": "release_readiness",
                "role": "release",
                "requirement_ids": requirement_ids,
                "depends_on": [*verification_ids, "documentation"],
                "artifact": "release_readiness.json",
            },
        ]
        plan = {
            "nodes": nodes,
            "entry_nodes": ["architecture"],
            "exit_nodes": ["release_readiness"],
            "requirement_ids": requirement_ids,
        }
        if self.provider is not None:
            response = self.provider.complete(
                "planner",
                "Return a JSON task graph with nodes, entry_nodes, exit_nodes, and requirement_ids. "
                "Each node requires id, role, requirement_ids, depends_on, and artifact. "
                "Roles must include architecture, implementation, testing, documentation, and release. "
                "Use distinct testing nodes per requirement, preserve all requirements, and return only JSON.",
                {"analysis": analysis, "baseline_plan": plan},
            )
            try:
                plan = json.loads(response)
            except json.JSONDecodeError as error:
                raise ValueError("Planner provider must return valid JSON.") from error
        if not self.validate(plan) or set(plan.get("requirement_ids", [])) != set(requirement_ids):
            raise ValueError("Generated task graph is cyclic or does not cover all requirements.")
        return plan

    @staticmethod
    def validate(plan):
        if not isinstance(plan, dict):
            return False
        nodes = plan.get("nodes", [])
        entry_nodes = plan.get("entry_nodes", [])
        exit_nodes = plan.get("exit_nodes", [])
        requirement_ids = plan.get("requirement_ids", [])
        if not isinstance(nodes, list) or not nodes:
            return False
        if (
            not isinstance(entry_nodes, list)
            or not isinstance(exit_nodes, list)
            or not isinstance(requirement_ids, list)
            or not all(isinstance(item, str) for item in entry_nodes + exit_nodes + requirement_ids)
        ):
            return False
        if any(
            not isinstance(node, dict)
            or not isinstance(node.get("id"), str)
            or not isinstance(node.get("role"), str)
            or not isinstance(node.get("artifact"), str)
            or Path(node["artifact"]).name != node["artifact"]
            or not isinstance(node.get("requirement_ids"), list)
            or not isinstance(node.get("depends_on"), list)
            or not all(isinstance(item, str) for item in node.get("requirement_ids", []))
            or not all(isinstance(item, str) for item in node.get("depends_on", []))
            for node in nodes
        ):
            return False
        node_ids = {node.get("id") for node in nodes}
        if len(node_ids) != len(nodes):
            return False
        if any(not set(node["depends_on"]).issubset(node_ids) for node in nodes):
            return False
        if not set(entry_nodes + exit_nodes).issubset(node_ids):
            return False
        planned_requirements = set(requirement_ids)
        if any(not set(node["requirement_ids"]).issubset(planned_requirements) for node in nodes):
            return False
        covered = {item for node in nodes for item in node["requirement_ids"]}
        if not planned_requirements.issubset(covered):
            return False
        for role in ("implementation", "testing", "release"):
            role_coverage = {
                item
                for node in nodes if node.get("role") == role
                for item in node["requirement_ids"]
            }
            if not planned_requirements.issubset(role_coverage):
                return False
        verification_nodes = [node for node in nodes if node["role"] == "testing"]
        verification_requirements = {
            tuple(node["requirement_ids"])
            for node in verification_nodes
        }
        if (
            len(verification_nodes) != len(planned_requirements)
            or verification_requirements != {(requirement_id,) for requirement_id in planned_requirements}
        ):
            return False

        role_map = {node["role"] for node in nodes}
        required_roles = {"architecture", "implementation", "testing", "documentation", "release"}
        if not required_roles.issubset(role_map) or not role_map.issubset(required_roles):
            return False
        if len({node["artifact"] for node in nodes}) != len(nodes):
            return False
        if any(Path(node["artifact"]).is_absolute() for node in nodes):
            return False
        if any(Path(node["artifact"]).name != node["artifact"] for node in nodes):
            return False
        roots = {node["id"] for node in nodes if not node["depends_on"]}
        if set(entry_nodes) != roots or not exit_nodes:
            return False
        if any(next(node for node in nodes if node["id"] == node_id)["role"] != "release" for node_id in exit_nodes):
            return False
        remaining = {node["id"]: set(node["depends_on"]) for node in nodes}
        completed = set()
        while remaining:
            ready = {node_id for node_id, dependencies in remaining.items() if dependencies.issubset(completed)}
            if not ready:
                return False
            completed.update(ready)
            for node_id in ready:
                del remaining[node_id]
        reachable = set(entry_nodes)
        while True:
            updated = reachable | {
                node["id"] for node in nodes
                if set(node["depends_on"]).issubset(reachable)
            }
            if updated == reachable:
                break
            reachable = updated
        if reachable != node_ids:
            return False
        can_reach_exit = set(exit_nodes)
        while True:
            updated = can_reach_exit | {
                node["id"] for node in nodes
                if any(node["id"] in dependent["depends_on"] for dependent in nodes if dependent["id"] in can_reach_exit)
            }
            if updated == can_reach_exit:
                break
            can_reach_exit = updated
        return can_reach_exit == node_ids


class SDLCArtifactAgent:
    def __init__(self, provider: AgentProvider | None = None):
        self.provider = provider

    def render(self, node, analysis):
        requirement_by_id = {item["id"]: item for item in analysis["requirements"]}
        selected = [requirement_by_id[item] for item in node["requirement_ids"]]
        role = node["role"]
        if role == "architecture":
            lines = ["# Architecture Design", "", f"Request: {analysis['raw_prompt']}", "", "## Requirements"]
            lines.extend(f"- {item['id']}: {item['description']}" for item in selected)
            lines.extend(["", "## Components", "- URL/API boundary", "- Persistence adapter", "- Policy and validation gates"])
        elif role == "implementation":
            project_root = Path(__file__).resolve().parents[1]
            implementation_path = project_root / "src" / "url_shortener.py"
            if self.provider is not None:
                source = self.provider.complete(
                    "implementation",
                    "Produce a complete Python WSGI URL-shortener module based on the original request, normalized requirements, and maintained source. "
                    "Explicit user constraints override defaults in the maintained source and normalized assumptions. "
                    "Implement every listed capability and constraint; if any cannot be satisfied, return an explicit JSON object with an unsupported_reason instead of code. "
                    "Use parameterized SQL and otherwise return only Python source.",
                    {
                        "raw_request": analysis["raw_prompt"],
                        "normalized_requirements": analysis["requirements"],
                        "requirements": selected,
                        "constraints": analysis.get("constraints", {}),
                        "assumptions": analysis["assumptions"],
                        "source": implementation_path.read_text(encoding="utf-8"),
                    },
                )
                try:
                    refusal = json.loads(source)
                except json.JSONDecodeError:
                    refusal = None
                if isinstance(refusal, dict) and refusal.get("unsupported_reason"):
                    raise ValueError(f"Implementation provider cannot satisfy the request: {refusal['unsupported_reason']}")
                try:
                    implementation_tree = ast.parse(source)
                except SyntaxError as error:
                    raise ValueError("Implementation provider returned invalid Python syntax.") from error
                if not any(
                    isinstance(item, ast.FunctionDef) and item.name == "create_app"
                    for item in implementation_tree.body
                ):
                    raise ValueError("Implementation provider output must define create_app.")
                if not PolicyGuardrailEngine.evaluate_code_safety(source):
                    raise ValueError("Implementation provider output failed the code safety policy.")
                return source if source.endswith("\n") else source + "\n"
            implementation_tree = ast.parse(implementation_path.read_text(encoding="utf-8"))
            factory = next(
                (item for item in implementation_tree.body if isinstance(item, ast.FunctionDef) and item.name == "create_app"),
                None,
            )
            if factory is None:
                raise ValueError("Maintained URL shortener source is missing create_app.")
            parameter_names = [argument.arg for argument in factory.args.args]
            try:
                capability_index = parameter_names.index("enabled_capabilities")
            except ValueError as error:
                raise ValueError("Maintained URL shortener source cannot be capability-configured.") from error
            default_index = capability_index - (len(parameter_names) - len(factory.args.defaults))
            if default_index < 0:
                raise ValueError("enabled_capabilities must have a default in the maintained application factory.")
            factory.args.defaults[default_index] = ast.Tuple(
                elts=[ast.Constant(value=item) for item in node["requirement_ids"]],
                ctx=ast.Load(),
            )
            default_offset = len(parameter_names) - len(factory.args.defaults)
            constraints = analysis.get("constraints", {})
            for parameter_name in ("default_expiration_hours", "max_expiration_hours"):
                if parameter_name in parameter_names:
                    parameter_index = parameter_names.index(parameter_name) - default_offset
                    if parameter_index >= 0:
                        factory.args.defaults[parameter_index] = ast.Constant(
                            value=constraints.get(parameter_name, 24)
                        )
            return ast.unparse(ast.fix_missing_locations(implementation_tree)) + "\n"
        elif role == "testing":
            return self.run_acceptance_tests(node, analysis)
        elif role == "documentation":
            lines = ["# Setup and Operations", "", f"Scope: {analysis['raw_prompt']}", "", "## Assumptions"]
            lines.extend(f"- {item}" for item in analysis["assumptions"])
            lines.extend(["", "## Open Questions"])
            lines.extend(f"- {item}" for item in analysis["unresolved_questions"])
        elif role == "release":
            test_result = analysis.get("acceptance_test_result", {})
            required_ids = set(node["requirement_ids"])
            validated_ids = set(test_result.get("validated_requirement_ids", []))
            app_path = Path(analysis["output_dir"]) / "generated_app.py"
            actual_hash = hashlib.sha256(app_path.read_bytes()).hexdigest()
            if not test_result.get("passed") or not required_ids.issubset(validated_ids):
                raise ValueError("Release readiness requires executed acceptance tests to pass.")
            if test_result.get("implementation_sha256") != actual_hash:
                raise ValueError("Release readiness blocked because tested implementation changed.")
            return {
                "passed": True,
                "requirement_count": len(selected),
                "validated_requirement_ids": sorted(required_ids),
                "checks": ["plan graph valid", "requirements covered", "architecture documented", "acceptance tests executed", "operations documented"],
                "test_result": analysis["acceptance_test_result"],
            }
        else:
            raise ValueError(f"Unsupported local agent role: {role}")
        return "\n".join(lines) + "\n"

    def run_acceptance_tests(self, node, analysis):
        output_dir = Path(analysis["output_dir"])
        app_path = output_dir / "generated_app.py"
        constraints = analysis.get("constraints", {})
        default_expiration_hours = constraints.get("default_expiration_hours", 24)
        max_expiration_hours = constraints.get("max_expiration_hours", default_expiration_hours)
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "acceptance-report.json"
            runner_path = Path(__file__).resolve().with_name("acceptance_runner.py")
            allowed_environment = {
                key: os.environ[key]
                for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR")
                if key in os.environ
            }
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        str(runner_path),
                        str(app_path.resolve()),
                        json.dumps(node["requirement_ids"]),
                        str(report_path),
                        str(default_expiration_hours),
                        str(max_expiration_hours),
                    ],
                    cwd=temp_dir,
                    env={**allowed_environment, "TMPDIR": temp_dir},
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
            except FileNotFoundError as error:
                raise ValueError("Python acceptance-test process could not be started.") from error
            except subprocess.TimeoutExpired as error:
                raise ValueError("Generated application acceptance tests exceeded 20 seconds.") from error
            if result.returncode != 0:
                details = (result.stderr or result.stdout)[-3000:]
                raise ValueError(f"Generated application acceptance tests failed: {details}")
            report = json.loads(report_path.read_text(encoding="utf-8"))
        report["implementation_sha256"] = hashlib.sha256(app_path.read_bytes()).hexdigest()
        return report

    @staticmethod
    def write_artifact(output_dir, node, content):
        artifact_path = Path(output_dir) / node["artifact"]
        if isinstance(content, dict):
            serialized_content = json.dumps(content, indent=2)
        else:
            serialized_content = content
        return PolicyGuardrailEngine.write_artifact(str(artifact_path), serialized_content, str(output_dir))



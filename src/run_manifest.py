import hashlib
import json
from pathlib import Path

from src.guardrails import PolicyGuardrailEngine


def write_run_manifest(
    output_dir,
    engine,
    request,
    normalized_requirements,
    artifact_names,
    inputs=None,
    manifest_name="run_manifest.json",
    provider=None,
):
    output_path = Path(output_dir).resolve()
    snapshot_dir = output_path / "run_artifacts" / engine.run_id
    artifact_records = []
    for name in sorted(set(artifact_names)):
        artifact_path = output_path / name
        if artifact_path.is_file():
            snapshot_path = snapshot_dir / name
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_bytes = artifact_path.read_bytes()
            snapshot_path.write_bytes(artifact_bytes)
            artifact_records.append({
                "path": name,
                "snapshot_path": str(snapshot_path.relative_to(output_path)),
                "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            })

    test_reports = []
    for artifact in artifact_records:
        if "validation" not in artifact["path"] and "test_report" not in artifact["path"]:
            continue
        try:
            report = json.loads((output_path / artifact["path"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        test_reports.append({"path": artifact["path"], "result": report})

    tasks = list(engine.tasks.values())
    provider_metadata = None
    if provider is not None:
        metadata_factory = getattr(provider, "manifest_metadata", None)
        provider_metadata = metadata_factory() if callable(metadata_factory) else {
            "name": provider.__class__.__name__,
        }
    manifest = {
        "run_id": engine.run_id,
        "scenario": engine.scenario_name,
        "started_at": engine.start_time,
        "finished_at": engine.end_time,
        "request": request,
        "normalized_requirements": normalized_requirements,
        "provider": provider_metadata,
        "inputs": inputs or {},
        "plan": [
            {"task_id": task.id, "name": task.name, "dependencies": task.dependencies}
            for task in tasks
        ],
        "approvals": [
            event for event in engine.audit_lineage
            if event["event"] in {
                "PENDING_APPROVAL", "APPROVED", "REJECTED", "APPROVAL_TIMEOUT",
                "APPROVAL_PROVIDER_ERROR",
            }
        ],
        "task_trace": [
            {
                "task_id": task.id,
                "name": task.name,
                "status": task.status,
                "inputs": task.input_context,
                "output": task.output,
                "error": task.error,
            }
            for task in tasks
        ],
        "code_changes": [
            artifact for artifact in artifact_records
            if Path(artifact["path"]).suffix.lower() in {".py", ".sql", ".diff"}
        ],
        "test_results": test_reports,
        "final_decision": {"passed": bool(engine.success), "success": engine.success},
        "metrics": engine.get_metrics(),
        "artifacts": artifact_records,
    }
    manifest_path = output_path / manifest_name
    PolicyGuardrailEngine.write_artifact(
        str(manifest_path),
        json.dumps(manifest, indent=2, default=str),
        str(output_path),
    )
    return manifest_path
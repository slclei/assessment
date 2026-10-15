import ast
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Optional

from src.orchestrator import OrchestratorEngine
from src.guardrails import PolicyGuardrailEngine
from src.run_manifest import write_run_manifest


_SECURE_LOOKUP_SOURCE = '''def lookup_url(connection, short_code):
    return connection.execute(
        "SELECT long_url FROM urls WHERE short_code = ?",
        (short_code,),
    ).fetchone()
'''


def _has_unsafe_query_concatenation(tree, function=None):
    return any(
        isinstance(node, ast.Assign)
        and isinstance(node.value, (ast.BinOp, ast.JoinedStr))
        and any(isinstance(target, ast.Name) and target.id == "query" for target in node.targets)
        for node in ast.walk(function if function is not None else tree)
    )


def _has_parameterized_lookup(tree, function=None):
    for node in ast.walk(function if function is not None else tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "execute" or len(node.args) != 2:
            continue
        query, parameters = node.args
        if (
            isinstance(query, ast.Constant)
            and isinstance(query.value, str)
            and "WHERE short_code = ?" in query.value
            and isinstance(parameters, ast.Tuple)
            and len(parameters.elts) == 1
            and isinstance(parameters.elts[0], ast.Name)
            and parameters.elts[0].id == "short_code"
        ):
            return True
    return False


def _is_expected_secure_lookup(tree):
    expected_tree = ast.parse(_SECURE_LOOKUP_SOURCE)
    lookup_function = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "lookup_url"),
        None,
    )
    expected_function = expected_tree.body[0]
    return lookup_function is not None and ast.dump(lookup_function, include_attributes=False) == ast.dump(
        expected_function,
        include_attributes=False,
    )


def run_brownfield_scenario(source_path: Optional[str] = None):
    engine = OrchestratorEngine(scenario_name="Brownfield-URL-Shortener-Refactor")
    output_dir = Path("output_artifacts").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_name = "brownfield_run_manifest.json"
    (output_dir / manifest_name).unlink(missing_ok=True)
    input_path = (
        Path(source_path).expanduser().resolve()
        if source_path
        else Path(__file__).resolve().parent / "fixtures" / "legacy_lookup.py"
    )
    if not input_path.is_file():
        raise FileNotFoundError(f"Brownfield source module not found: {input_path}")
    input_sha256 = hashlib.sha256(input_path.read_bytes()).hexdigest()
    request = "Replace the vulnerable lookup_url SQL interpolation with a parameterized query."
    engine.context["request"] = request
    engine.context["input_module"] = str(input_path)
    engine.context["input_sha256"] = input_sha256
    audit_path = output_dir / "legacy_audit.json"
    patched_path = output_dir / "secure_lookup.py"
    validation_path = output_dir / "brownfield_validation.json"

    def write_artifact(path, content):
        if isinstance(content, dict):
            content = json.dumps(content, indent=2)
        return PolicyGuardrailEngine.write_artifact(str(path), content, str(output_dir))

    def cleanup_artifacts(ctx):
        for path in (audit_path, patched_path, validation_path):
            path.unlink(missing_ok=True)

    def audit_legacy_module(ctx):
        source = input_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(input_path))
        lookup_functions = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "lookup_url"
        ]
        if len(lookup_functions) != 1:
            raise ValueError("Legacy input module must define exactly one top-level lookup_url function.")
        if not _has_unsafe_query_concatenation(tree, lookup_functions[0]):
            raise ValueError("Expected legacy SQL query construction was not found.")
        report = {
            "input_module": str(input_path),
            "parsed": True,
            "findings": ["SQL injection risk: short_code is interpolated into SQL text."],
        }
        write_artifact(audit_path, report)
        ctx["legacy_source"] = source
        return report

    def patch_legacy_module(ctx):
        source = ctx["legacy_source"]
        tree = ast.parse(source, filename=str(input_path))
        lookup_functions = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "lookup_url"
        ]
        if len(lookup_functions) != 1 or not _has_unsafe_query_concatenation(tree, lookup_functions[0]):
            raise ValueError("Could not safely locate the audited vulnerable lookup function.")
        function = lookup_functions[0]
        if function.decorator_list:
            raise ValueError("Decorated lookup_url functions require manual review before replacement.")
        source_lines = source.splitlines(keepends=True)
        newline = "\r\n" if "\r\n" in source else "\n"
        replacement_lines = _SECURE_LOOKUP_SOURCE.rstrip("\n").splitlines()
        replacement = newline.join(replacement_lines)
        function_ends_with_newline = source_lines[function.end_lineno - 1].endswith(("\n", "\r"))
        if function.end_lineno < len(source_lines) or function_ends_with_newline:
            replacement += newline
        patched_source = "".join(
            source_lines[:function.lineno - 1]
            + [replacement]
            + source_lines[function.end_lineno:]
        )
        patched_tree = ast.parse(patched_source, filename=str(patched_path))
        patched_functions = [
            node for node in patched_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "lookup_url"
        ]
        if (
            len(patched_functions) != 1
            or not _has_parameterized_lookup(patched_tree, patched_functions[0])
            or not _is_expected_secure_lookup(patched_tree)
        ):
            raise ValueError("Patched module does not match the approved parameterized lookup implementation.")
        compile(patched_tree, str(patched_path), "exec")
        write_artifact(patched_path, patched_source)
        return {"patched_module": "output_artifacts/secure_lookup.py", "change": "Generated an isolated, parameterized lookup replacement."}

    def validate_refactor(ctx):
        source = patched_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(patched_path))
        lookup_functions = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "lookup_url"
        ]
        if (
            len(lookup_functions) != 1
            or not _has_parameterized_lookup(tree, lookup_functions[0])
            or not _is_expected_secure_lookup(tree)
        ):
            raise ValueError("Security validation failed: replacement differs from the approved parameterized lookup.")
        namespace = {}
        isolated_function = ast.Module(body=[lookup_functions[0]], type_ignores=[])
        exec(compile(isolated_function, str(patched_path), "exec"), {"__builtins__": {}}, namespace)
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE TABLE urls (short_code TEXT, long_url TEXT)")
            connection.execute("INSERT INTO urls VALUES (?, ?)", ("known", "https://example.com/known"))
            normal_result = namespace["lookup_url"](connection, "known")
            injection_result = namespace["lookup_url"](connection, "missing' OR '1'='1")
        if normal_result is None or normal_result[0] != "https://example.com/known":
            raise ValueError("Patched lookup failed the normal query check.")
        if injection_result is not None:
            raise ValueError("Patched lookup returned a row for injection-shaped input.")
        if not audit_path.is_file() or not patched_path.is_file():
            raise ValueError("Brownfield output artifacts are missing.")
        report = {
            "passed": True,
            "input_module": str(input_path),
            "patched_module": str(patched_path),
            "checks": ["AST parse", "Python compile", "parameterized query", "normal lookup", "injection-shaped lookup rejected"],
        }
        report["patched_module"] = "output_artifacts/secure_lookup.py"
        write_artifact(validation_path, report)
        return report

    audit_task = engine.add_task("Parse and Audit Legacy Lookup", audit_legacy_module)
    patch_task = engine.add_task(
        "Patch SQL Injection with Bound Parameters",
        patch_legacy_module,
        dependencies=[audit_task],
        requires_approval=True,
        approval_details="Replace only lookup_url with a parameterized short_code query; preserve the rest of the caller's module.",
        compensate_action=cleanup_artifacts,
    )
    engine.add_task("Compile and Verify Refactored Module", validate_refactor, dependencies=[patch_task])

    success = engine.execute()
    write_run_manifest(
        output_dir,
        engine,
        request,
        ["parameterized_lookup"],
        ["legacy_audit.json", "secure_lookup.py", "brownfield_validation.json"],
        inputs={"source_module": str(input_path), "source_sha256": input_sha256},
        manifest_name=manifest_name,
    )
    return engine, success and validation_path.is_file()
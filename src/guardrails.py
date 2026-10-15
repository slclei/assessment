import ast
from pathlib import Path


class PolicyGuardrailEngine:
    """Formal policy compliance and change control guardrails."""
    
    @staticmethod
    def validate_schema_compliance(context: dict) -> bool:
        spec = context.get("spec", "")
        # Enforce compliance: block unencrypted or destructive operations
        if "DROP DATABASE" in spec.upper() or "UNENCRYPTED" in spec.upper():
            return False
        return True

    @staticmethod
    def validate_file_operation(filepath: str, allowed_root: str = None) -> bool:
        path = Path(filepath).resolve()
        restricted_extensions = {".env", ".pem", ".key", ".secret"}
        if path.suffix.lower() in restricted_extensions:
            return False
        if path.name.lower() in {".env", "secrets.json", "credentials.json"}:
            return False
        if allowed_root is not None:
            root = Path(allowed_root).resolve()
            if path != root and root not in path.parents:
                return False
        return True

    @staticmethod
    def evaluate_code_safety(code_snippet: str) -> bool:
        try:
            tree = ast.parse(code_snippet)
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec", "compile", "__import__"}:
                return False
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"os", "subprocess"}
                and node.func.attr in {"system", "popen", "Popen", "run", "call", "check_call", "check_output"}
            ):
                return False
        return True

    @staticmethod
    def write_artifact(filepath: str, content: str, allowed_root: str) -> Path:
        artifact_path = Path(filepath).resolve()
        if not PolicyGuardrailEngine.validate_file_operation(str(artifact_path), allowed_root):
            raise PermissionError(f"Artifact path is outside the permitted output directory: {artifact_path}")
        if artifact_path.suffix.lower() == ".py":
            if not PolicyGuardrailEngine.evaluate_code_safety(content):
                raise PermissionError(f"Generated Python artifact failed the code safety policy: {artifact_path}")
            compile(content, str(artifact_path), "exec")
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(content, encoding="utf-8")
        return artifact_path
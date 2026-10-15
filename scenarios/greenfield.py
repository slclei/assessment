import importlib.util
import io
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from src.orchestrator import OrchestratorEngine
from src.guardrails import PolicyGuardrailEngine
from src.run_manifest import write_run_manifest


def _call_wsgi(application, method, path, payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    response = {}
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "HTTP_HOST": "localhost:8000",
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
    }
    response_body = b"".join(application(environ, lambda status, headers: response.update(
        status=status, headers=dict(headers)
    )))
    response["body"] = response_body
    return response


def run_greenfield_scenario():
    engine = OrchestratorEngine(scenario_name="Greenfield-URL-Shortener")
    output_dir = Path("output_artifacts").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_name = "greenfield_run_manifest.json"
    (output_dir / manifest_name).unlink(missing_ok=True)
    db_path = "urls.db"
    request = "Build a URL shortener with redirects and click analytics."
    engine.context["request"] = request

    def generate_api_spec(ctx):
        specification = {
            "openapi": "3.1.0",
            "info": {"title": "URL Shortener API", "version": "1.0.0"},
            "servers": [{"url": "http://localhost:8000"}],
            "paths": {
                "/shorten": {
                    "post": {
                        "operationId": "createShortUrl",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ShortenRequest"}
                                }
                            },
                        },
                        "responses": {
                            "201": {
                                "description": "Short URL created.",
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/ShortenResponse"}
                                    }
                                },
                            },
                            "400": {"description": "Invalid request."},
                            "409": {"description": "Custom alias already exists."},
                            "413": {"description": "Request body is too large."},
                            "503": {"description": "Short-code allocation failed."},
                        },
                    }
                },
                "/{short_code}": {
                    "get": {
                        "operationId": "redirectShortUrl",
                        "parameters": [{
                            "name": "short_code",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
                        }],
                        "responses": {
                            "302": {
                                "description": "Redirect to the original URL.",
                                "headers": {"Location": {"schema": {"type": "string", "format": "uri"}}},
                            },
                            "404": {"description": "Short URL not found."},
                        },
                    }
                },
                "/analytics/{short_code}": {
                    "get": {
                        "operationId": "getShortUrlAnalytics",
                        "parameters": [{
                            "name": "short_code",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
                        }],
                        "responses": {
                            "200": {
                                "description": "Aggregate click analytics.",
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/ClickAnalytics"}
                                    }
                                },
                            },
                            "404": {"description": "Short URL not found."},
                        },
                    }
                },
            },
            "components": {
                "schemas": {
                    "ShortenRequest": {
                        "type": "object",
                        "required": ["long_url"],
                        "properties": {
                            "long_url": {"type": "string", "format": "uri", "maxLength": 2048},
                            "custom_alias": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,32}$"},
                        },
                    },
                    "ShortenResponse": {
                        "type": "object",
                        "required": ["short_code", "short_url"],
                        "properties": {
                            "short_code": {"type": "string"},
                            "short_url": {"type": "string", "format": "uri"},
                        },
                    },
                    "ClickAnalytics": {
                        "type": "object",
                        "required": ["short_code", "click_count", "last_clicked_at"],
                        "properties": {
                            "short_code": {"type": "string"},
                            "click_count": {"type": "integer", "minimum": 0},
                            "last_clicked_at": {"type": ["string", "null"]},
                        },
                    },
                }
            },
        }
        spec = json.dumps(specification, indent=2)
        ctx["spec"] = spec
        PolicyGuardrailEngine.write_artifact("output_artifacts/openapi.json", spec, str(output_dir))
        return "Generated OpenAPI specification for URL shortener."

    def generate_standard_server_app(ctx):
        implementation_path = Path(__file__).resolve().parents[1] / "src" / "url_shortener.py"
        implementation = implementation_path.read_text(encoding="utf-8")
        PolicyGuardrailEngine.write_artifact("output_artifacts/main.py", implementation, str(output_dir))
        return "Generated runnable URL shortener service implementation."

    def generate_architecture_design(ctx):
        design = """# URL Shortener Architecture

## Components
- WSGI HTTP boundary handles shortening, redirect, and analytics requests.
- SQLite stores short-code mappings and click events.
- The orchestration layer gates schema migration and validates generated artifacts.

## Data Flow
Shorten requests are validated before persistence. Redirect requests resolve a code and record a click in one operation. Analytics reports aggregate counts without visitor identifiers.
"""
        PolicyGuardrailEngine.write_artifact("output_artifacts/architecture.md", design, str(output_dir))
        return "output_artifacts/architecture.md"

    def generate_database_schema(ctx):
        schema = '''-- SQLite Schema for URL Shortener
CREATE TABLE IF NOT EXISTS urls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    short_code TEXT UNIQUE NOT NULL,
    long_url TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS click_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    short_code TEXT NOT NULL REFERENCES urls(short_code) ON DELETE CASCADE,
    clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
'''
        PolicyGuardrailEngine.write_artifact("output_artifacts/schema.sql", schema, str(output_dir))
        return "Generated SQLite database schema script."

    def execute_real_migration(ctx):
        created_tables = []
        with closing(sqlite3.connect(db_path)) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing_tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if "urls" not in existing_tables:
                    conn.execute('''
                        CREATE TABLE urls (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            short_code TEXT UNIQUE NOT NULL,
                            long_url TEXT NOT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    created_tables.append("urls")
                if "click_events" not in existing_tables:
                    conn.execute('''
                        CREATE TABLE click_events (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            short_code TEXT NOT NULL REFERENCES urls(short_code) ON DELETE CASCADE,
                            clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    created_tables.append("click_events")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        ctx["greenfield_created_tables"] = created_tables
        return {"created_tables": created_tables}

    def rollback_database_migration(ctx):
        created_tables = set(ctx.get("greenfield_created_tables", []))
        if created_tables and os.path.exists(db_path):
            with closing(sqlite3.connect(db_path)) as conn:
                try:
                    conn.execute("PRAGMA foreign_keys = ON")
                    conn.execute("BEGIN IMMEDIATE")
                    if "click_events" in created_tables:
                        conn.execute("DROP TABLE IF EXISTS click_events")
                    if "urls" in created_tables:
                        conn.execute("DROP TABLE IF EXISTS urls")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        ctx["greenfield_created_tables"] = []

    def cleanup_generated_artifacts(ctx):
        for path in [
            "output_artifacts/openapi.json",
            "output_artifacts/main.py",
            "output_artifacts/schema.sql",
            "output_artifacts/architecture.md",
            "output_artifacts/setup_and_operations.md",
            "output_artifacts/greenfield_validation.json",
            "output_artifacts/release_readiness.json",
            "output_artifacts/run_manifest.json",
        ]:
            if os.path.exists(path):
                os.remove(path)

    def generate_documentation(ctx):
        documentation = """# URL Shortener Setup and Operations

Run from the project directory with `python output_artifacts/main.py` after approving the SQLite migration.

## Endpoints
- `POST /shorten` creates a short code.
- `GET /{short_code}` redirects and records a click.
- `GET /analytics/{short_code}` returns aggregate click counts.

## Operational Notes
Data is stored in `urls.db`. Back up the database before schema changes. The prototype does not collect visitor identifiers.
"""
        PolicyGuardrailEngine.write_artifact("output_artifacts/setup_and_operations.md", documentation, str(output_dir))
        return "output_artifacts/setup_and_operations.md"

    def validate_release_readiness(ctx):
        required_artifacts = [
            "output_artifacts/openapi.json",
            "output_artifacts/main.py",
            "output_artifacts/schema.sql",
            "output_artifacts/architecture.md",
            "output_artifacts/setup_and_operations.md",
            "output_artifacts/greenfield_validation.json",
        ]
        if any(not os.path.isfile(path) or os.path.getsize(path) == 0 for path in required_artifacts):
            raise ValueError("Release readiness failed because a required SDLC artifact is missing.")
        with open("output_artifacts/greenfield_validation.json", encoding="utf-8") as artifact:
            validation = json.load(artifact)
        if not validation.get("passed"):
            raise ValueError("Release readiness failed because generated service validation did not pass.")
        report = {"passed": True, "validated_artifacts": required_artifacts}
        PolicyGuardrailEngine.write_artifact("output_artifacts/release_readiness.json", json.dumps(report, indent=2), str(output_dir))
        return report

    def validate_generated_artifacts(ctx):
        with open("output_artifacts/openapi.json", encoding="utf-8") as artifact:
            specification = json.load(artifact)
        with open("output_artifacts/schema.sql", encoding="utf-8") as artifact:
            schema = artifact.read()
        with open("output_artifacts/main.py", encoding="utf-8") as artifact:
            app_source = artifact.read()
        compile(app_source, "output_artifacts/main.py", "exec")

        required_operations = {
            ("/shorten", "post", "createShortUrl"),
            ("/{short_code}", "get", "redirectShortUrl"),
            ("/analytics/{short_code}", "get", "getShortUrlAnalytics"),
        }
        if specification.get("openapi") != "3.1.0" or not specification.get("info", {}).get("title"):
            raise ValueError("Generated API contract is not an OpenAPI 3.1 document.")
        for path, method, operation_id in required_operations:
            operation = specification.get("paths", {}).get(path, {}).get(method, {})
            if operation.get("operationId") != operation_id or not operation.get("responses"):
                raise ValueError(f"OpenAPI operation is incomplete: {method.upper()} {path}.")
        shorten_request = specification["paths"]["/shorten"]["post"].get("requestBody", {})
        if not shorten_request.get("required") or "application/json" not in shorten_request.get("content", {}):
            raise ValueError("OpenAPI shorten operation is missing its required JSON request body.")
        if "302" not in specification["paths"]["/{short_code}"]["get"]["responses"]:
            raise ValueError("OpenAPI redirect operation is missing its 302 response.")
        if "CREATE TABLE IF NOT EXISTS click_events" not in schema:
            raise ValueError("Generated schema is missing click-event storage.")

        module_spec = importlib.util.spec_from_file_location("generated_url_shortener", "output_artifacts/main.py")
        if module_spec is None or module_spec.loader is None:
            raise ValueError("Could not load generated URL shortener module.")
        generated_module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(generated_module)
        with tempfile.TemporaryDirectory() as temp_dir:
            application = generated_module.create_app(os.path.join(temp_dir, "validation.db"))
            first = _call_wsgi(application, "POST", "/shorten", {"long_url": "https://example.com/one"})
            second = _call_wsgi(application, "POST", "/shorten", {"long_url": "https://example.com/two"})
            first_code = json.loads(first["body"])["short_code"]
            second_code = json.loads(second["body"])["short_code"]
            if not first["status"].startswith("201") or not second["status"].startswith("201") or first_code == second_code:
                raise ValueError("Generated shorten endpoint failed validation.")
            for code, expected_url in ((first_code, "https://example.com/one"), (second_code, "https://example.com/two")):
                redirect = _call_wsgi(application, "GET", f"/{code}")
                if not redirect["status"].startswith("302") or redirect["headers"].get("Location") != expected_url:
                    raise ValueError("Generated redirect endpoint failed validation.")
            analytics = _call_wsgi(application, "GET", f"/analytics/{first_code}")
            missing = _call_wsgi(application, "GET", "/unknown-code")
            if not analytics["status"].startswith("200") or json.loads(analytics["body"])["click_count"] != 1:
                raise ValueError("Generated analytics endpoint failed validation.")
            if not missing["status"].startswith("404"):
                raise ValueError("Generated service did not return 404 for an unknown code.")

        report = {"passed": True, "checks": ["syntax", "API specification", "schema", "shorten", "unique codes", "redirects", "analytics", "404"]}
        PolicyGuardrailEngine.write_artifact("output_artifacts/greenfield_validation.json", json.dumps(report, indent=2), str(output_dir))
        return report

    t1 = engine.add_task(
        "Normalize URL Shortener Spec",
        generate_api_spec,
        compensate_action=cleanup_generated_artifacts,
    )
    architecture_task = engine.add_task("Architecture Design", generate_architecture_design, dependencies=[t1])
    t2 = engine.add_task("Parallel Server Implementation", generate_standard_server_app, dependencies=[architecture_task])
    t3 = engine.add_task("Parallel SQLite Schema Gen", generate_database_schema, dependencies=[architecture_task])
    
    migration_task = engine.add_task(
        "Production Database Migration", 
        action=execute_real_migration,
        compensate_action=rollback_database_migration,
        dependencies=[t2, t3],
        requires_approval=True,
        approval_details="Create only missing urls and click_events tables in urls.db. Existing tables and rows are preserved.",
    )
    validation_task = engine.add_task(
        "Validate Greenfield Artifacts",
        validate_generated_artifacts,
        dependencies=[migration_task],
    )
    documentation_task = engine.add_task(
        "Generate Setup and Operations Documentation",
        generate_documentation,
        dependencies=[validation_task],
    )
    release_task = engine.add_task(
        "Validate Release Readiness",
        validate_release_readiness,
        dependencies=[documentation_task],
    )
    
    success = engine.execute()
    write_run_manifest(
        output_dir,
        engine,
        request,
        ["shorten_urls", "redirect_urls", "click_analytics"],
        [
            "openapi.json", "main.py", "schema.sql", "architecture.md",
            "setup_and_operations.md", "greenfield_validation.json", "release_readiness.json",
        ],
        inputs={"database_path": db_path},
        manifest_name=manifest_name,
    )
    return engine, success and os.path.isfile("output_artifacts/release_readiness.json")
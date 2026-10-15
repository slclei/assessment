import json
import importlib.util
import json
import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scenarios.ambiguous import run_ambiguous_scenario
from scenarios.brownfield import run_brownfield_scenario
from scenarios.greenfield import _call_wsgi, run_greenfield_scenario
from src.local_agents import SDLCArtifactAgent


class TestScenarioArtifacts(unittest.TestCase):
    def run_scenario_in_temp_directory(self, scenario, validate_artifacts, approval="y"):
        original_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                with patch("src.orchestrator.read_line_with_deadline", return_value=approval):
                    engine, success = scenario()
                self.assertTrue(success)
                self.assertTrue(engine.get_metrics()["success"])
                validate_artifacts(Path(temp_dir) / "output_artifacts")
            finally:
                os.chdir(original_directory)

    def test_greenfield_validates_generated_api_artifacts(self):
        def validate(artifacts):
            report = json.loads((artifacts / "greenfield_validation.json").read_text(encoding="utf-8"))
            release = json.loads((artifacts / "release_readiness.json").read_text(encoding="utf-8"))
            manifest = json.loads((artifacts / "greenfield_run_manifest.json").read_text(encoding="utf-8"))
            implementation = (artifacts / "main.py").read_text(encoding="utf-8")
            specification = json.loads((artifacts / "openapi.json").read_text(encoding="utf-8"))
            self.assertTrue(report["passed"])
            self.assertIn("analytics", report["checks"])
            self.assertTrue(release["passed"])
            self.assertTrue(manifest["final_decision"]["passed"])
            self.assertEqual(manifest["normalized_requirements"], ["shorten_urls", "redirect_urls", "click_analytics"])
            self.assertTrue(manifest["approvals"])
            self.assertTrue(manifest["test_results"])
            self.assertTrue(any(item["inputs"].get("request") == manifest["request"] for item in manifest["task_trace"]))
            main_artifact = next(item for item in manifest["artifacts"] if item["path"] == "main.py")
            self.assertEqual(
                main_artifact["sha256"],
                hashlib.sha256((artifacts / main_artifact["snapshot_path"]).read_bytes()).hexdigest(),
            )
            self.assertTrue((artifacts / "architecture.md").is_file())
            self.assertEqual(specification["openapi"], "3.1.0")
            self.assertEqual(specification["info"]["title"], "URL Shortener API")
            self.assertEqual(
                specification["paths"]["/shorten"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/ShortenRequest",
            )
            self.assertIn("302", specification["paths"]["/{short_code}"]["get"]["responses"])
            self.assertIn("200", specification["paths"]["/analytics/{short_code}"]["get"]["responses"])
            self.assertTrue((artifacts / "setup_and_operations.md").is_file())
            self.assertIn("default_expiration_hours=24", implementation)
            self.assertIn("CREATE TABLE IF NOT EXISTS click_events", implementation)
            with sqlite3.connect(artifacts.parent / "urls.db") as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )}
            self.assertTrue({"urls", "click_events"}.issubset(tables))

        self.run_scenario_in_temp_directory(run_greenfield_scenario, validate)

    def test_greenfield_migration_rejection_cleans_generated_artifacts(self):
        original_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                with closing(sqlite3.connect("urls.db")) as connection:
                    with connection:
                        connection.execute("CREATE TABLE unrelated_data (value TEXT)")
                        connection.execute("INSERT INTO unrelated_data VALUES (?)", ("preserve-me",))
                        connection.execute("CREATE TABLE urls (short_code TEXT, long_url TEXT)")
                        connection.execute("INSERT INTO urls VALUES (?, ?)", ("existing", "https://example.com/keep"))
                with patch("src.orchestrator.read_line_with_deadline", return_value="n"):
                    engine, success = run_greenfield_scenario()
                self.assertFalse(success)
                self.assertFalse(engine.get_metrics()["success"])
                self.assertTrue(Path("urls.db").exists())
                with closing(sqlite3.connect("urls.db")) as connection:
                    with connection:
                        self.assertEqual(connection.execute("SELECT value FROM unrelated_data").fetchone()[0], "preserve-me")
                        self.assertEqual(connection.execute("SELECT long_url FROM urls WHERE short_code = 'existing'").fetchone()[0], "https://example.com/keep")
                        tables = {row[0] for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )}
                self.assertNotIn("click_events", tables)
                self.assertFalse((Path("output_artifacts") / "openapi.json").exists())
                self.assertFalse((Path("output_artifacts") / "main.py").exists())
                self.assertFalse((Path("output_artifacts") / "schema.sql").exists())
            finally:
                os.chdir(original_directory)

    def test_migration_rollback_removes_only_tables_created_by_run(self):
        original_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                with closing(sqlite3.connect("urls.db")) as connection:
                    with connection:
                        connection.execute("CREATE TABLE unrelated_data (value TEXT)")
                        connection.execute("INSERT INTO unrelated_data VALUES (?)", ("preserve-me",))
                        connection.execute("CREATE TABLE urls (short_code TEXT, long_url TEXT)")
                        connection.execute("INSERT INTO urls VALUES (?, ?)", ("existing", "https://example.com/keep"))
                with patch("src.orchestrator.read_line_with_deadline", return_value="y"):
                    engine, success = run_greenfield_scenario()
                self.assertTrue(success)
                migration_task = next(
                    task for task in engine.tasks.values()
                    if task.name == "Production Database Migration"
                )

                migration_task.compensate_action(engine.context)

                self.assertTrue(Path("urls.db").exists())
                with closing(sqlite3.connect("urls.db")) as connection:
                    with connection:
                        self.assertEqual(connection.execute("SELECT value FROM unrelated_data").fetchone()[0], "preserve-me")
                        self.assertEqual(connection.execute("SELECT long_url FROM urls WHERE short_code = 'existing'").fetchone()[0], "https://example.com/keep")
                        tables = {row[0] for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )}
                self.assertNotIn("click_events", tables)
            finally:
                os.chdir(original_directory)

    def test_brownfield_patches_real_input_and_validates_security(self):
        def validate(artifacts):
            audit = json.loads((artifacts / "legacy_audit.json").read_text(encoding="utf-8"))
            report = json.loads((artifacts / "brownfield_validation.json").read_text(encoding="utf-8"))
            manifest = json.loads((artifacts / "brownfield_run_manifest.json").read_text(encoding="utf-8"))
            patched_module = (artifacts / "secure_lookup.py").read_text(encoding="utf-8")
            self.assertTrue(audit["parsed"])
            self.assertIn("SQL injection risk", audit["findings"][0])
            self.assertTrue(report["passed"])
            self.assertTrue(manifest["final_decision"]["passed"])
            self.assertEqual(
                manifest["inputs"]["source_sha256"],
                hashlib.sha256(Path(manifest["inputs"]["source_module"]).read_bytes()).hexdigest(),
            )
            self.assertIn("WHERE short_code = ?", patched_module)

        self.run_scenario_in_temp_directory(run_brownfield_scenario, validate)

    def test_brownfield_accepts_a_caller_supplied_module(self):
        original_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                source_path = Path("custom_legacy_lookup.py").resolve()
                source_path.write_text(
                    "def keep_existing_helper(value):\n"
                    "    return value.strip()\n\n"
                    "def lookup_url(connection, short_code):\n"
                    "    query = f\"SELECT long_url FROM urls WHERE short_code = '{short_code}'\"\n"
                    "    return connection.execute(query).fetchone()\n",
                    encoding="utf-8",
                )
                with patch("src.orchestrator.read_line_with_deadline", return_value="y"):
                    engine, success = run_brownfield_scenario(str(source_path))
                self.assertTrue(success)
                self.assertTrue(engine.get_metrics()["success"])
                audit = json.loads(Path("output_artifacts/legacy_audit.json").read_text(encoding="utf-8"))
                self.assertEqual(audit["input_module"], str(source_path))
                patched_source = Path("output_artifacts/secure_lookup.py").read_text(encoding="utf-8")
                self.assertIn("def keep_existing_helper(value):", patched_source)
                self.assertIn("return value.strip()", patched_source)
                self.assertTrue(Path("output_artifacts/brownfield_validation.json").is_file())
            finally:
                os.chdir(original_directory)

    def test_brownfield_never_executes_top_level_input_code(self):
        original_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                source_path = Path("custom_legacy_lookup.py").resolve()
                source_path.write_text(
                    "from pathlib import Path\n"
                    "Path('untrusted_side_effect').write_text('executed')\n"
                    "def lookup_url(connection, short_code):\n"
                    "    query = f\"SELECT long_url FROM urls WHERE short_code = '{short_code}'\"\n"
                    "    return connection.execute(query).fetchone()\n",
                    encoding="utf-8",
                )
                with patch("src.orchestrator.read_line_with_deadline", return_value="y"):
                    engine, success = run_brownfield_scenario(str(source_path))

                self.assertTrue(success)
                self.assertFalse(Path("untrusted_side_effect").exists())
                patched_source = Path("output_artifacts/secure_lookup.py").read_text(encoding="utf-8")
                self.assertIn("Path('untrusted_side_effect').write_text('executed')", patched_source)
            finally:
                os.chdir(original_directory)

    def test_ambiguous_captures_questions_and_validates_graph(self):
        def validate(artifacts):
            clarification = json.loads((artifacts / "intent_clarification.json").read_text(encoding="utf-8"))
            graph = json.loads((artifacts / "task_breakdown.json").read_text(encoding="utf-8"))
            report = json.loads((artifacts / "ambiguous_final_validation.json").read_text(encoding="utf-8"))
            acceptance = json.loads((artifacts / "acceptance_test_report.json").read_text(encoding="utf-8"))
            release = json.loads((artifacts / "release_readiness.json").read_text(encoding="utf-8"))
            approval = json.loads((artifacts / "stakeholder_approval.json").read_text(encoding="utf-8"))
            manifest = json.loads((artifacts / "ambiguous_run_manifest.json").read_text(encoding="utf-8"))
            replanning = json.loads((artifacts / "replanning_demo.json").read_text(encoding="utf-8"))
            self.assertTrue(clarification["unresolved_questions"])
            self.assertTrue(clarification["assumptions"])
            self.assertEqual(graph["exit_nodes"], ["release_readiness"])
            self.assertIn("architecture.md", report["validated_artifacts"])
            self.assertIn("acceptance_test_report.json", report["validated_artifacts"])
            self.assertIn("setup_and_operations.md", report["validated_artifacts"])
            self.assertIn("implementation.diff", release["validated_artifacts"])
            self.assertTrue(report["passed"])
            self.assertTrue(acceptance["passed"])
            self.assertEqual(manifest["request"], clarification["raw_prompt"])
            self.assertEqual(manifest["normalized_requirements"], graph["requirement_ids"])
            self.assertTrue(manifest["final_decision"]["passed"])
            self.assertTrue(manifest["approvals"])
            self.assertTrue(manifest["test_results"])
            review_approval = next(
                event for event in manifest["approvals"]
                if event["event"] == "PENDING_APPROVAL"
            )
            self.assertIn("src/url_shortener.py", review_approval["details"])
            self.assertTrue(any(item["path"] == "implementation.diff" for item in manifest["code_changes"]))
            self.assertEqual(approval["release_artifact"], "output_artifacts/release_readiness.json")
            self.assertEqual(
                set(acceptance["validated_requirement_ids"]),
                set(graph["requirement_ids"]),
            )
            verification_nodes = [node for node in graph["nodes"] if node["role"] == "testing"]
            self.assertEqual(
                {item for node in verification_nodes for item in node["requirement_ids"]},
                set(graph["requirement_ids"]),
            )
            self.assertTrue(all(node["depends_on"] == ["implementation"] for node in verification_nodes))
            self.assertEqual(len({node["artifact"] for node in verification_nodes}), len(verification_nodes))
            self.assertIn("both short codes redirect to their destinations", acceptance["checks"])
            self.assertIn("custom aliases are accepted and collisions rejected", acceptance["checks"])
            self.assertTrue(replanning["passed"])
            self.assertEqual(replanning["verified_expiry_values"], [24, 168])
            self.assertEqual(replanning["compensation_order"], ["release", "verification"])
            self.assertIn("OUTPUT_REVISED", replanning["revision_events"])
            self.assertTrue(manifest["inputs"]["replanning_demo"]["passed"])
            self.assertTrue(any(
                "Review the seven-day lifetime change" in event["details"]
                for event in replanning["approvals"]
            ))
            self.assertTrue((artifacts / "generated_app.py").is_file())

        self.run_scenario_in_temp_directory(run_ambiguous_scenario, validate)

    def test_ambiguous_prompt_changes_the_planned_graph(self):
        prompt = "Shorten URLs, redirect them, track clicks, and support custom aliases."

        def validate(artifacts):
            clarification = json.loads((artifacts / "intent_clarification.json").read_text(encoding="utf-8"))
            graph = json.loads((artifacts / "task_breakdown.json").read_text(encoding="utf-8"))
            self.assertEqual(clarification["raw_prompt"], prompt)
            self.assertEqual(
                set(graph["requirement_ids"]),
                {"shorten_urls", "redirect_urls", "click_analytics", "custom_aliases"},
            )
            implementation_node = next(node for node in graph["nodes"] if node["id"] == "implementation")
            self.assertIn("custom_aliases", implementation_node["requirement_ids"])
            verification_nodes = [node for node in graph["nodes"] if node["role"] == "testing"]
            self.assertEqual(
                {item for node in verification_nodes for item in node["requirement_ids"]},
                set(graph["requirement_ids"]),
            )
            self.assertEqual(len(verification_nodes), len(graph["requirement_ids"]))
            implementation = (Path("output_artifacts") / "generated_app.py").read_text(encoding="utf-8")
            self.assertIn("def create_app", implementation)
            self.assertTrue((artifacts / "generated_app.py").is_file())

        self.run_scenario_in_temp_directory(lambda: run_ambiguous_scenario(prompt), validate)

    def test_ambiguous_expiration_request_generates_and_validates_new_behavior(self):
        prompt = "Build a URL shortener with expiring links after 24 hours."

        def validate(artifacts):
            analysis = json.loads((artifacts / "intent_clarification.json").read_text(encoding="utf-8"))
            graph = json.loads((artifacts / "task_breakdown.json").read_text(encoding="utf-8"))
            report = json.loads((artifacts / "acceptance_test_report_expiring_links.json").read_text(encoding="utf-8"))
            implementation = (artifacts / "generated_app.py").read_text(encoding="utf-8")
            release = json.loads((artifacts / "release_readiness.json").read_text(encoding="utf-8"))
            self.assertIn("expiring_links", {item["id"] for item in analysis["requirements"]})
            self.assertIn("expiring_links", graph["requirement_ids"])
            self.assertIn("expires_at", implementation)
            self.assertTrue(report["passed"])
            self.assertIn("new links expire by default after 24 hours; overrides are bounded and expired redirects return 410", report["checks"])
            self.assertTrue(release["passed"])
            diff = (artifacts / "implementation.diff").read_text(encoding="utf-8")
            self.assertIn("expiring_links", diff)
            self.assertTrue(json.loads((artifacts / "replanning_demo.json").read_text(encoding="utf-8"))["passed"])

        self.run_scenario_in_temp_directory(
            lambda: run_ambiguous_scenario(prompt),
            validate,
        )

    def test_ambiguous_explicit_seven_day_default_is_generated_and_verified(self):
        prompt = "Build a URL shortener and make the default lifetime seven days."

        def validate(artifacts):
            analysis = json.loads((artifacts / "intent_clarification.json").read_text(encoding="utf-8"))
            report = json.loads((artifacts / "acceptance_test_report_expiring_links.json").read_text(encoding="utf-8"))
            implementation = (artifacts / "generated_app.py").read_text(encoding="utf-8")
            self.assertEqual(analysis["constraints"]["default_expiration_hours"], 168)
            self.assertIn("default_expiration_hours=168", implementation)
            self.assertTrue(report["passed"])
            self.assertIn(
                "new links expire by default after 168 hours; overrides are bounded and expired redirects return 410",
                report["checks"],
            )

        self.run_scenario_in_temp_directory(
            lambda: run_ambiguous_scenario(prompt),
            validate,
        )

    def test_ambiguous_generated_service_limits_features_to_the_plan(self):
        prompt = "Shorten URLs."

        def validate(artifacts):
            graph = json.loads((artifacts / "task_breakdown.json").read_text(encoding="utf-8"))
            self.assertEqual(set(graph["requirement_ids"]), {"shorten_urls", "redirect_urls"})
            module_spec = importlib.util.spec_from_file_location(
                "scoped_generated_url_shortener",
                artifacts / "generated_app.py",
            )
            generated_app = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(generated_app)
            with tempfile.TemporaryDirectory() as temp_dir:
                application = generated_app.create_app(str(Path(temp_dir) / "scoped.db"))
                created = _call_wsgi(application, "POST", "/shorten", {
                    "long_url": "https://example.com/scoped",
                })
                self.assertTrue(created["status"].startswith("201"))
                short_code = json.loads(created["body"])["short_code"]
                alias = _call_wsgi(application, "POST", "/shorten", {
                    "long_url": "https://example.com/alias",
                    "custom_alias": "not-enabled",
                })
                analytics = _call_wsgi(application, "GET", f"/analytics/{short_code}")
                self.assertTrue(alias["status"].startswith("400"))
                self.assertTrue(analytics["status"].startswith("404"))

        self.run_scenario_in_temp_directory(lambda: run_ambiguous_scenario(prompt), validate)

    def test_ambiguous_blocks_unsupported_authentication_requirement(self):
        original_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                with patch("src.orchestrator.read_line_with_deadline", return_value="y"):
                    engine, success = run_ambiguous_scenario(
                        "Add authentication and authorization to the URL shortener."
                    )

                self.assertFalse(success)
                self.assertFalse(engine.get_metrics()["success"])
                analysis = json.loads(Path("output_artifacts/intent_clarification.json").read_text(encoding="utf-8"))
                blocked = json.loads(Path("output_artifacts/unsupported_requirements.json").read_text(encoding="utf-8"))
                self.assertIn("security_controls", {item["id"] for item in analysis["unsupported_requirements"]})
                self.assertTrue(blocked["blocked"])
                self.assertFalse(Path("output_artifacts/release_readiness.json").exists())
            finally:
                os.chdir(original_directory)

    def test_ambiguous_blocks_unrecognized_capabilities_mixed_with_supported_scope(self):
        for prompt in (
            "Shorten URLs with rate limiting.",
            "Shorten URLs and add billing.",
        ):
            with self.subTest(prompt=prompt):
                original_directory = os.getcwd()
                with tempfile.TemporaryDirectory() as temp_dir:
                    os.chdir(temp_dir)
                    try:
                        with patch("src.orchestrator.read_line_with_deadline", return_value="y"):
                            engine, success = run_ambiguous_scenario(prompt)

                        self.assertFalse(success)
                        self.assertFalse(engine.get_metrics()["success"])
                        analysis = json.loads(Path("output_artifacts/intent_clarification.json").read_text(encoding="utf-8"))
                        self.assertIn(
                            "unsupported_scope",
                            {item["id"] for item in analysis["unsupported_requirements"]},
                        )
                        self.assertFalse(Path("output_artifacts/release_readiness.json").exists())
                        manifest = json.loads(Path("output_artifacts/ambiguous_run_manifest.json").read_text(encoding="utf-8"))
                        self.assertFalse(manifest["final_decision"]["passed"])
                    finally:
                        os.chdir(original_directory)

    def test_release_rejects_implementation_changed_after_acceptance_tests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "generated_app.py"
            app_path.write_text("def create_app():\n    return None\n", encoding="utf-8")
            node = {"role": "release", "requirement_ids": ["shorten_urls"]}
            analysis = {
                "requirements": [{
                    "id": "shorten_urls",
                    "description": "Create unique short codes for valid URLs.",
                }],
                "output_dir": temp_dir,
                "acceptance_test_result": {
                    "passed": True,
                    "validated_requirement_ids": ["shorten_urls"],
                    "implementation_sha256": "stale-hash",
                },
            }

            with self.assertRaisesRegex(ValueError, "implementation changed"):
                SDLCArtifactAgent().render(node, analysis)


if __name__ == "__main__":
    unittest.main()
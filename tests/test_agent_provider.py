import io
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.agent_provider import OpenAICompatibleProvider, configured_agent_provider
from src.local_agents import LocalTaskPlannerAgent, RequirementAnalystAgent, SDLCArtifactAgent
from src.run_manifest import write_run_manifest


class TestAgentProvider(unittest.TestCase):
    def test_configured_provider_requires_complete_environment(self):
        self.assertIsNone(configured_agent_provider({}))
        with self.assertRaisesRegex(ValueError, "Missing provider configuration"):
            configured_agent_provider({"AGENT_PROVIDER": "openai-compatible"})

    def test_provider_rejects_remote_plaintext_endpoint(self):
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            OpenAICompatibleProvider("http://model.example", "key", "model")

    def test_provider_sends_role_and_payload_and_returns_content(self):
        response_body = json.dumps({
            "choices": [{"message": {"content": "generated result"}}],
        }).encode("utf-8")
        response = io.BytesIO(response_body)
        response.__enter__ = lambda: response
        response.__exit__ = lambda *args: None
        provider = OpenAICompatibleProvider(
            "https://model.example/v1", "secret-value", "assessment-model"
        )

        with patch("src.agent_provider.urllib.request.urlopen", return_value=response) as open_url:
            result = provider.complete("planner", "Return JSON.", {"prompt": "shorten URLs"})

        self.assertEqual(result, "generated result")
        request = open_url.call_args.args[0]
        request_body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request_body["messages"][0]["content"], "Return JSON.")
        self.assertEqual(
            json.loads(request_body["messages"][1]["content"]),
            {"agent_role": "planner", "prompt": "shorten URLs"},
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-value")

    def test_provider_drives_analysis_planning_and_implementation_roles(self):
        class FakeProvider:
            def __init__(self):
                self.roles = []
                self.implementation_payload = None

            def complete(self, role, instructions, payload):
                self.roles.append(role)
                if role == "analyst":
                    return json.dumps({
                        "requirement_ids": ["expiring_links"],
                        "unsupported_requirements": [],
                        "unresolved_questions": ["What expiry should be the default?"],
                        "assumptions": ["Expiration is opt-in."],
                    })
                if role == "planner":
                    return json.dumps(payload["baseline_plan"])
                self.implementation_payload = payload
                return "def create_app():\n    return None\n"

        provider = FakeProvider()
        analysis = RequirementAnalystAgent(provider).analyze(
            "Shorten URLs and allow links to expire after 24 hours."
        )
        plan = LocalTaskPlannerAgent(provider).plan(analysis)
        implementation_node = next(node for node in plan["nodes"] if node["role"] == "implementation")
        source = SDLCArtifactAgent(provider).render(implementation_node, analysis)

        self.assertIn("expiring_links", {item["id"] for item in analysis["requirements"]})
        self.assertTrue(LocalTaskPlannerAgent.validate(plan))
        self.assertEqual(source, "def create_app():\n    return None\n")
        self.assertEqual(provider.roles, ["analyst", "planner", "implementation"])
        self.assertEqual(
            provider.implementation_payload["raw_request"],
            "Shorten URLs and allow links to expire after 24 hours.",
        )
        self.assertEqual(provider.implementation_payload["constraints"]["default_expiration_hours"], 24)

    def test_explicit_seven_day_default_reaches_implementation_provider(self):
        class CapturingProvider:
            def __init__(self):
                self.implementation_payload = None

            def complete(self, role, instructions, payload):
                if role == "analyst":
                    return json.dumps({
                        "requirement_ids": ["shorten_urls"],
                        "unsupported_requirements": [],
                        "unresolved_questions": [],
                        "assumptions": [],
                    })
                if role == "planner":
                    return json.dumps(payload["baseline_plan"])
                self.implementation_payload = payload
                return "def create_app():\n    return None\n"

        prompt = "Make the default lifetime seven days for the URL shortener."
        provider = CapturingProvider()
        analysis = RequirementAnalystAgent(provider).analyze(prompt)
        plan = LocalTaskPlannerAgent(provider).plan(analysis)
        implementation_node = next(node for node in plan["nodes"] if node["role"] == "implementation")
        SDLCArtifactAgent(provider).render(implementation_node, analysis)

        self.assertEqual(analysis["constraints"]["default_expiration_hours"], 168)
        self.assertIn("expiring_links", {item["id"] for item in analysis["requirements"]})
        self.assertTrue(any("168-hour default lifetime" in item for item in analysis["assumptions"]))
        self.assertFalse(any("24-hour default lifetime" in item for item in analysis["assumptions"]))
        self.assertEqual(provider.implementation_payload["raw_request"], prompt)
        self.assertEqual(provider.implementation_payload["constraints"]["default_expiration_hours"], 168)

    def test_natural_seven_day_default_prompt_is_not_marked_unsupported(self):
        analysis = RequirementAnalystAgent().analyze(
            "Set the default lifetime to 7 days for new links in my URL shortener."
        )

        self.assertEqual(analysis["constraints"]["default_expiration_hours"], 168)
        self.assertEqual(analysis["unsupported_requirements"], [])

    def test_provider_manifest_metadata_redacts_endpoint_secrets(self):
        provider = OpenAICompatibleProvider(
            "https://user:password@model.example/v1?token=query-secret#fragment",
            "api-key-secret",
            "assessment-model",
        )

        metadata = provider.manifest_metadata()

        self.assertEqual(metadata, {
            "name": "openai-compatible",
            "model": "assessment-model",
            "endpoint": "https://model.example/v1/chat/completions",
        })
        self.assertNotIn("api-key-secret", json.dumps(metadata))
        self.assertNotIn("query-secret", json.dumps(metadata))

    def test_run_manifest_records_only_sanitized_provider_metadata(self):
        provider = OpenAICompatibleProvider(
            "https://user:password@model.example/v1?token=query-secret",
            "api-key-secret",
            "assessment-model",
        )
        engine = SimpleNamespace(
            run_id="test-run",
            scenario_name="test-scenario",
            start_time=None,
            end_time=None,
            tasks={},
            audit_lineage=[],
            success=False,
            get_metrics=lambda: {},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = write_run_manifest(
                temp_dir,
                engine,
                "request",
                [],
                [],
                provider=provider,
            )
            manifest_text = manifest_path.read_text(encoding="utf-8")

        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["provider"]["name"], "openai-compatible")
        self.assertEqual(manifest["provider"]["model"], "assessment-model")
        self.assertEqual(manifest["provider"]["endpoint"], "https://model.example/v1/chat/completions")
        self.assertNotIn("api-key-secret", manifest_text)
        self.assertNotIn("password", manifest_text)
        self.assertNotIn("query-secret", manifest_text)

    def test_implementation_provider_can_explicitly_reject_unsupported_constraint(self):
        class RefusingProvider:
            def complete(self, role, instructions, payload):
                if role == "implementation":
                    return json.dumps({"unsupported_reason": "The requested behavior cannot be validated."})
                return "{}"

        analysis = {
            "raw_prompt": "Make the default lifetime seven days.",
            "requirements": [{"id": "expiring_links", "description": "Expiry", "supported": True}],
            "assumptions": [],
            "constraints": {"default_expiration_hours": 168, "max_expiration_hours": 168},
        }
        node = {"role": "implementation", "requirement_ids": ["expiring_links"]}

        with self.assertRaisesRegex(ValueError, "cannot satisfy the request"):
            SDLCArtifactAgent(RefusingProvider()).render(node, analysis)

    def test_provider_cannot_ignore_unsupported_prompt_scope(self):
        class UnderReportingProvider:
            def complete(self, role, instructions, payload):
                return json.dumps({
                    "requirement_ids": ["shorten_urls"],
                    "unsupported_requirements": [],
                    "unresolved_questions": [],
                    "assumptions": [],
                })

        analysis = RequirementAnalystAgent(UnderReportingProvider()).analyze(
            "Shorten URLs and add billing."
        )

        self.assertTrue(analysis["unsupported_requirements"])
        self.assertIn("billing", analysis["unsupported_requirements"][0]["description"])

    def test_provider_plan_with_disconnected_node_is_rejected(self):
        class DisconnectedPlanProvider:
            def complete(self, role, instructions, payload):
                plan = payload["baseline_plan"]
                plan["nodes"].append({
                    "id": "orphan",
                    "role": "architecture",
                    "requirement_ids": [],
                    "depends_on": [],
                    "artifact": "orphan.md",
                })
                plan["entry_nodes"].append("orphan")
                return json.dumps(plan)

        analysis = RequirementAnalystAgent().analyze("Shorten URLs.")
        with self.assertRaisesRegex(ValueError, "task graph"):
            LocalTaskPlannerAgent(DisconnectedPlanProvider()).plan(analysis)


if __name__ == "__main__":
    unittest.main()
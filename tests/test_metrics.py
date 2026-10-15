import json
import tempfile
import unittest

from src.models import FailurePolicy
from src.orchestrator import OrchestratorEngine


class TestRunMetrics(unittest.TestCase):
    def test_success_rate_aggregates_recorded_runs_per_scenario(self):
        with tempfile.TemporaryDirectory() as output_dir:
            first_run = OrchestratorEngine("Metrics-Scenario", output_dir=output_dir)
            first_run.add_task("Success", lambda context: "ok")
            self.assertTrue(first_run.execute())
            first_metrics = first_run.get_metrics()
            self.assertEqual(first_metrics["recorded_runs"], 1)
            self.assertEqual(first_metrics["successful_runs"], 1)
            self.assertEqual(first_metrics["success_rate_percentage"], 100.0)
            self.assertIsNone(first_metrics["mttr_sec"])
            self.assertGreater(first_metrics["end_to_end_latency_sec"], 0)
            with open(first_run.audit_file, encoding="utf-8") as audit_file:
                first_run_events = [json.loads(line) for line in audit_file]
            self.assertTrue(any(event["event"] == "COMPLETED" for event in first_run_events))

            failed_run = OrchestratorEngine("Metrics-Scenario", output_dir=output_dir)
            failed_run.add_task("Failure", lambda context: "not run", max_retries=0)
            self.assertFalse(failed_run.execute())
            metrics = failed_run.get_metrics()
            self.assertEqual(metrics["recorded_runs"], 2)
            self.assertEqual(metrics["successful_runs"], 1)
            self.assertEqual(metrics["success_rate_percentage"], 50.0)
            self.assertIsNone(metrics["mttr_sec"])
            with open(failed_run.audit_file, encoding="utf-8") as audit_file:
                audit_events = [json.loads(line) for line in audit_file]
            self.assertTrue(any(event["run_id"] == first_run.run_id for event in audit_events))
            self.assertTrue(any(event["run_id"] == failed_run.run_id for event in audit_events))

            with open(failed_run.history_file, encoding="utf-8") as history_file:
                history = json.load(history_file)
            self.assertEqual(len(history["runs"]), 2)
            with open(failed_run.state_file, encoding="utf-8") as state_file:
                state = json.load(state_file)
            self.assertEqual(state["persistence_mode"], "audit_only")

            other_scenario = OrchestratorEngine("Other-Scenario", output_dir=output_dir)
            other_scenario.add_task("Success", lambda context: "ok")
            self.assertTrue(other_scenario.execute())
            self.assertEqual(failed_run.get_metrics()["recorded_runs"], 2)
            self.assertEqual(failed_run.get_metrics()["success_rate_percentage"], 50.0)

    def test_mttr_spans_first_failure_through_workflow_recovery(self):
        with tempfile.TemporaryDirectory() as output_dir:
            engine = OrchestratorEngine("MTTR-Scenario", output_dir=output_dir)

            def action(context):
                if not context.get("mitigated"):
                    raise RuntimeError("temporary upstream failure")
                return "recovered"

            def mitigate(context, error):
                context["mitigated"] = True

            engine.add_task(
                "Recoverable Task",
                action,
                max_retries=1,
                failure_policy=FailurePolicy.REPLAN,
                replan_action=mitigate,
            )
            self.assertTrue(engine.execute())
            metrics = engine.get_metrics()

            self.assertGreater(metrics["mttr_sec"], 0)
            self.assertLessEqual(metrics["failure_timestamp"], metrics["recovery_timestamp"])
            self.assertEqual(metrics["recorded_runs"], 1)
            self.assertEqual(metrics["total_retries"], 1)
            self.assertGreater(metrics["end_to_end_latency_sec"], 0)
            with open(engine.history_file, encoding="utf-8") as history_file:
                record = json.load(history_file)["runs"][0]
            self.assertEqual(record["recovery_timestamp"], record["end_timestamp"])
            self.assertEqual(record["mttr_sec"], metrics["mttr_sec"])
            self.assertGreater(record["mttr_sec"], 0)


if __name__ == "__main__":
    unittest.main()
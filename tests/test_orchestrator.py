import unittest
import io
import os
import threading
import tempfile
import time
from unittest.mock import patch
from src.approval import read_line_with_deadline
from src.orchestrator import OrchestratorEngine
from src.models import FailurePolicy, TaskOutputRevision, TaskStatus

class TestOrchestratorEngine(unittest.TestCase):
    def setUp(self):
        self.output_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.output_dir.cleanup()

    def make_engine(self, **kwargs):
        return OrchestratorEngine(output_dir=self.output_dir.name, **kwargs)

    def test_dag_dependencies_and_success(self):
        engine = self.make_engine(scenario_name="Test-DAG")
        t1 = engine.add_task("Step 1", lambda ctx: "Output 1")
        t2 = engine.add_task("Step 2", lambda ctx: "Output 2", dependencies=[t1])
        success = engine.execute()
        self.assertTrue(success)

    def test_policy_guardrail_block(self):
        engine = self.make_engine(scenario_name="Test-Policy")
        engine.add_task(
            "Unsafe Task", 
            action=lambda ctx: "Unsafe", 
            policy_check=lambda ctx: False # Triggers policy block
        )
        success = engine.execute()
        self.assertFalse(success)

    def test_dynamic_replan_recovery(self):
        engine = self.make_engine(scenario_name="Test-Replan-Recovery")

        def failing_action(ctx):
            if not ctx.get("scope_replanned"):
                raise RuntimeError("Intentional scope shift")
            return "recovered output"

        def mitigate_scope(ctx, error_msg):
            ctx["scope_replanned"] = True
            return f"Mitigated: {error_msg}"

        task_id = engine.add_task(
            "Unstable Task", action=failing_action, max_retries=1,
            failure_policy=FailurePolicy.REPLAN, replan_action=mitigate_scope
        )
        dependent_outputs = []
        engine.add_task(
            "Dependent Task",
            action=lambda ctx: dependent_outputs.append(engine.tasks[task_id].output) or "done",
            dependencies=[task_id]
        )
        
        success = engine.execute()
        self.assertTrue(success, "Dynamic re-planning should successfully recover the workflow.")
        self.assertTrue(engine.get_metrics()["success"])
        self.assertEqual(engine.tasks[task_id].status, TaskStatus.COMPLETED.value)
        self.assertEqual(engine.tasks[task_id].output, "recovered output")
        self.assertEqual(engine.tasks[task_id].replan_count, 1)
        self.assertEqual(dependent_outputs, ["recovered output"])
        self.assertEqual(len([task for task in engine.tasks.values() if task.name.startswith("Adaptive Re-planning")]), 1)

    def test_upstream_output_revision_compensates_and_reruns_dependents(self):
        engine = self.make_engine(scenario_name="Test-Upstream-Output-Revision")
        approvals = []
        engine.approval_provider = lambda prompt, timeout: approvals.append(prompt) or "y"
        observed_outputs = []
        compensations = []
        upstream_id = engine.add_task("Architecture Decision", lambda context: "v1")

        def consume_output(context):
            observed_outputs.append(engine.tasks[upstream_id].output)
            return observed_outputs[-1]

        consumer_id = engine.add_task(
            "Implementation From Decision",
            consume_output,
            dependencies=[upstream_id],
            compensate_action=lambda context: compensations.append("implementation"),
            requires_approval=True,
        )
        engine.add_task(
            "Stakeholder Decision Revision",
            lambda context: TaskOutputRevision(upstream_id, "v2", "stakeholder changed the accepted scope"),
            dependencies=[consumer_id],
            requires_approval=True,
                    may_revise_output=True,
        )

        self.assertTrue(engine.execute())
        self.assertEqual(observed_outputs, ["v1", "v2"])
        self.assertEqual(compensations, ["implementation"])
        self.assertEqual(len(approvals), 3)
        self.assertEqual(engine.tasks[upstream_id].output, "v2")
        self.assertEqual(engine.tasks[consumer_id].status, TaskStatus.COMPLETED.value)
        self.assertEqual(engine.tasks[upstream_id].revision_count, 1)
        self.assertTrue(any(event["event"] == "OUTPUT_REVISED" for event in engine.audit_lineage))

    def test_output_revision_capability_requires_approval_up_front(self):
        engine = self.make_engine(scenario_name="Test-Revision-Approval-Declaration")

        with self.assertRaisesRegex(ValueError, "must require human approval"):
            engine.add_task(
                "Unapproved Revision Task",
                lambda context: None,
                may_revise_output=True,
            )

    def test_output_revision_fails_closed_without_dependent_compensation(self):
        engine = self.make_engine(
            scenario_name="Test-Unsafe-Output-Revision",
            approval_provider=lambda prompt, timeout: "y",
        )
        upstream_id = engine.add_task("Upstream", lambda context: "v1")
        dependent_id = engine.add_task(
            "Uncompensated Dependent",
            lambda context: "derived",
            dependencies=[upstream_id],
        )
        engine.add_task(
            "Change Upstream",
            lambda context: TaskOutputRevision(upstream_id, "v2", "scope changed"),
            dependencies=[dependent_id],
                    requires_approval=True,
                    may_revise_output=True,
        )

        self.assertFalse(engine.execute())
        self.assertEqual(engine.tasks[upstream_id].output, "v1")
        self.assertTrue(any(event["event"] == "REPLAN_REJECTED" for event in engine.audit_lineage))

    def test_policy_rejection_halts_workflow(self):
        engine = self.make_engine(scenario_name="Test-Policy-Halt")
        
        # Task that violates policy guardrail
        engine.add_task(
            "Forbidden Migration",
            action=lambda ctx: "Should not execute",
            policy_check=lambda ctx: False,
            failure_policy=FailurePolicy.REPLAN,
            replan_action=lambda ctx, error: ctx.update({"should_not_replan": True})
        )
        
        success = engine.execute()
        self.assertFalse(success, "Policy denial must immediately halt the workflow, not recover via replanning.")
        self.assertEqual(len(engine.tasks), 1)

    def test_approval_rejection_halts_without_replanning(self):
        engine = self.make_engine(
            scenario_name="Test-Approval-Rejection",
            approval_provider=lambda prompt, timeout: "n",
        )
        compensation_calls = []
        action_calls = []
        setup_task = engine.add_task(
            "Prepare Change", action=lambda ctx: "prepared",
            compensate_action=lambda ctx: compensation_calls.append("rolled back")
        )
        protected_task = engine.add_task(
            "Protected Change", action=lambda ctx: action_calls.append("executed"),
            dependencies=[setup_task], requires_approval=True,
            failure_policy=FailurePolicy.REPLAN,
            replan_action=lambda ctx, error: ctx.update({"should_not_replan": True})
        )

        success = engine.execute()

        self.assertFalse(success)
        self.assertEqual(action_calls, [])
        self.assertEqual(compensation_calls, ["rolled back"])
        self.assertEqual(engine.tasks[setup_task].status, TaskStatus.ROLLED_BACK.value)
        self.assertEqual(engine.tasks[protected_task].status, TaskStatus.FAILED.value)
        self.assertEqual(len(engine.tasks), 2)

    def test_ready_approval_gates_are_serialized_before_actions(self):
        prompts = []
        action_calls = []

        def answer_prompt(prompt, timeout):
            prompts.append((prompt, threading.current_thread().name, timeout))
            return "y" if len(prompts) == 1 else "n"

        engine = self.make_engine(
            scenario_name="Test-Serialized-Approvals",
            approval_provider=answer_prompt,
        )

        first_task = engine.add_task(
            "First Protected Action",
            action=lambda context: action_calls.append("first"),
            requires_approval=True,
        )
        second_task = engine.add_task(
            "Second Protected Action",
            action=lambda context: action_calls.append("second"),
            requires_approval=True,
        )

        success = engine.execute()

        self.assertFalse(success)
        self.assertEqual(len(prompts), 2)
        self.assertEqual([thread_name for _, thread_name, _ in prompts], ["ApprovalProvider", "ApprovalProvider"])
        self.assertIn("First Protected Action", prompts[0][0])
        self.assertIn("Second Protected Action", prompts[1][0])
        self.assertEqual(action_calls, [])
        self.assertEqual(engine.tasks[first_task].status, TaskStatus.FAILED.value)
        self.assertEqual(engine.tasks[second_task].status, TaskStatus.FAILED.value)

    def test_approval_prompt_includes_change_and_prerequisite_details(self):
        prompts = []
        engine = self.make_engine(
            scenario_name="Test-Approval-Context",
            approval_provider=lambda prompt, timeout: prompts.append(prompt) or "y",
        )
        prerequisite = engine.add_task(
            "Prepare schema",
            lambda context: {"tables": ["urls", "click_events"]},
        )
        engine.add_task(
            "Apply database migration",
            lambda context: "migrated",
            dependencies=[prerequisite],
            requires_approval=True,
            approval_details="Create only missing tables; preserve existing rows.",
        )

        self.assertTrue(engine.execute())
        self.assertIn("Create only missing tables", prompts[0])
        self.assertIn("Prepare schema", prompts[0])
        self.assertIn("click_events", prompts[0])
        approval_request = next(
            event for event in engine.audit_lineage
            if event["event"] == "PENDING_APPROVAL"
        )
        self.assertIn("Create only missing tables", approval_request["details"])

    def test_rollback_compensates_in_reverse_dependency_order(self):
        engine = self.make_engine(scenario_name="Test-Rollback-Dependency-Order")
        compensations = []
        parent = engine.add_task(
            "Create parent resource",
            lambda context: "parent",
            compensate_action=lambda context: compensations.append("parent"),
        )
        child = engine.add_task(
            "Create dependent resource",
            lambda context: "child",
            dependencies=[parent],
            compensate_action=lambda context: compensations.append("child"),
        )

        def fail(context):
            raise RuntimeError("force rollback")

        engine.add_task("Fail after side effects", fail, dependencies=[child], max_retries=1)

        self.assertFalse(engine.execute())
        self.assertEqual(compensations, ["child", "parent"])

    def test_unresponsive_approval_provider_cannot_exceed_workflow_deadline(self):
        provider_started = threading.Event()
        release_provider = threading.Event()
        action_calls = []

        def unresponsive_provider(prompt, timeout):
            provider_started.set()
            release_provider.wait()
            return "y"

        engine = self.make_engine(
            scenario_name="Test-Unresponsive-Approval-Provider",
            max_execution_timeout=0.05,
            approval_provider=unresponsive_provider,
        )
        engine.add_task(
            "Protected Action",
            action=lambda context: action_calls.append("executed"),
            requires_approval=True,
        )

        start_time = time.monotonic()
        self.assertFalse(engine.execute())
        elapsed = time.monotonic() - start_time
        release_provider.set()

        self.assertTrue(provider_started.is_set())
        self.assertLess(elapsed, 0.3)
        self.assertEqual(action_calls, [])

    def test_approval_timeout_uses_remaining_workflow_budget(self):
        observed_timeouts = []
        action_calls = []

        def no_response_before_deadline(prompt, timeout):
            observed_timeouts.append(timeout)
            threading.Event().wait(timeout)
            return None

        engine = self.make_engine(
            scenario_name="Test-Approval-Timeout",
            max_execution_timeout=0.1,
            approval_provider=no_response_before_deadline,
        )
        task_id = engine.add_task(
            "Timed Approval",
            action=lambda context: action_calls.append("executed"),
            requires_approval=True,
        )

        start_time = time.monotonic()
        success = engine.execute()
        elapsed = time.monotonic() - start_time

        self.assertFalse(success)
        self.assertEqual(len(observed_timeouts), 1)
        self.assertGreater(observed_timeouts[0], 0)
        self.assertLessEqual(observed_timeouts[0], 0.101)
        self.assertLess(elapsed, 0.35)
        self.assertEqual(action_calls, [])
        self.assertEqual(engine.tasks[task_id].status, TaskStatus.FAILED.value)

    def test_default_approval_reader_returns_at_deadline(self):
        start_time = time.monotonic()
        with patch("sys.stdout", new_callable=io.StringIO):
            if os.name == "nt":
                with patch("msvcrt.kbhit", return_value=False):
                    response = read_line_with_deadline("Approval?", 0.03)
            else:
                with patch("select.select", return_value=([], [], [])):
                    response = read_line_with_deadline("Approval?", 0.03)
        elapsed = time.monotonic() - start_time

        self.assertIsNone(response)
        self.assertLess(elapsed, 0.2)

    def test_failure_after_side_effect_runs_compensation(self):
        engine = self.make_engine(scenario_name="Test-Failure-Rollback")
        effects = []
        prepare_task = engine.add_task(
            "Create Side Effect",
            action=lambda context: effects.append("created"),
            compensate_action=lambda context: effects.append("compensated"),
        )

        def fail_after_prepare(context):
            raise RuntimeError("downstream action failed")

        engine.add_task(
            "Failing Follow-up",
            fail_after_prepare,
            dependencies=[prepare_task],
            max_retries=1,
        )

        self.assertFalse(engine.execute())
        self.assertEqual(effects, ["created", "compensated"])
        self.assertEqual(engine.tasks[prepare_task].status, TaskStatus.ROLLED_BACK.value)

    def test_timeout_cancels_cooperative_blocking_task(self):
        engine = self.make_engine(
            scenario_name="Test-Cooperative-Timeout",
            max_execution_timeout=0.1,
        )
        started = threading.Event()
        stopped = threading.Event()

        def blocking_action(context):
            started.set()
            try:
                context.cancel_event.wait()
                context.check_cancelled()
            finally:
                stopped.set()

        task_id = engine.add_task("Blocking action", blocking_action, max_retries=1)
        start_time = time.monotonic()
        success = engine.execute()
        elapsed = time.monotonic() - start_time

        self.assertFalse(success)
        self.assertTrue(started.is_set())
        self.assertLess(elapsed, 0.5)
        self.assertTrue(stopped.wait(0.5))
        self.assertEqual(engine.tasks[task_id].status, TaskStatus.FAILED.value)

    def test_timeout_skips_rollback_while_worker_remains_active(self):
        engine = self.make_engine(
            scenario_name="Test-Timeout-Active-Worker",
            max_execution_timeout=0.1,
        )
        release_worker = threading.Event()
        worker_stopped = threading.Event()
        rollback_calls = []

        engine.add_task(
            "Completed Side Effect",
            action=lambda context: "created",
            compensate_action=lambda context: rollback_calls.append("rolled back"),
        )

        def unresponsive_worker(context):
            try:
                release_worker.wait()
                return "finished"
            finally:
                worker_stopped.set()

        engine.add_task("Unresponsive Worker", unresponsive_worker, max_retries=1)
        self.assertFalse(engine.execute())
        self.assertEqual(rollback_calls, [])
        self.assertEqual(engine.get_metrics()["total_rollbacks"], 0)

        release_worker.set()
        self.assertTrue(worker_stopped.wait(0.5))
        with open(engine.audit_file, encoding="utf-8") as audit_file:
            events = [line for line in audit_file if "SAFE_STOP_INCOMPLETE" in line]
        self.assertTrue(events)

if __name__ == "__main__":
    unittest.main()
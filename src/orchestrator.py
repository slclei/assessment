import os
import time
import json
import uuid
import logging
import queue
import threading
import copy
from dataclasses import asdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import List, Dict, Any, Callable, Optional, Set, Tuple
from src.approval import read_line_with_deadline
from src.models import ExecutionCancelledError, ExecutionContext, FailurePolicy, Task, TaskOutputRevision, TaskStatus
from src.guardrails import PolicyGuardrailEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("GovAgentOrchestrator")
_RUN_HISTORY_LOCK = threading.Lock()
CANCELLATION_DRAIN_TIMEOUT_SEC = 0.1

class OrchestratorEngine:
    def __init__(self, scenario_name: str, output_dir: str = "data", state_filename: str = "orchestrator_state.json", max_execution_timeout: float = 30.0, history_filename: str = "run_history.json", approval_provider: Optional[Callable[[str, float], Optional[str]]] = None):
        self.scenario_name = scenario_name
        self.output_dir = os.path.abspath(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)
        self.state_file = os.path.join(self.output_dir, state_filename)
        self.history_file = os.path.join(self.output_dir, history_filename)
        self.audit_file = os.path.join(self.output_dir, "audit_lineage.jsonl")
        self.max_execution_timeout = max_execution_timeout
        self.approval_provider = approval_provider or read_line_with_deadline
        
        self.tasks: Dict[str, Task] = {}
        self.context: ExecutionContext = ExecutionContext()
        self.audit_lineage: List[Dict[str, Any]] = []
        
        # Thread lock for safe concurrent state persistence
        self._state_lock = threading.Lock()
        
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self._run_start_perf = time.perf_counter()
        self._run_end_perf: Optional[float] = None
        self._failure_perf: Optional[float] = None
        self._recovery_perf: Optional[float] = None
        self.run_id: Optional[str] = None
        self.success: Optional[bool] = None
        self._run_recorded = False
        self.total_retries = 0
        self.total_rollbacks = 0
        self.failure_timestamp: Optional[float] = None
        self.recovery_timestamp: Optional[float] = None

    def add_task(self, name: str, action: Callable[..., Any], compensate_action: Optional[Callable[..., Any]] = None, dependencies: List[str] = None, requires_approval: bool = False, policy_check: Optional[Callable[..., bool]] = None, max_retries: int = 3, failure_policy: FailurePolicy = FailurePolicy.FATAL, replan_action: Optional[Callable[..., Any]] = None, max_replans: int = 1, may_revise_output: bool = False, approval_details: Optional[str] = None) -> str:
        if may_revise_output and not requires_approval:
            raise ValueError("Tasks that may revise upstream output must require human approval.")
        if failure_policy is FailurePolicy.REPLAN and replan_action is None:
            raise ValueError("REPLAN failure policy requires a replan_action callback.")
        if max_replans < 0:
            raise ValueError("max_replans cannot be negative.")

        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = Task(
            id=task_id, name=name, action=action, compensate_action=compensate_action,
            dependencies=dependencies or [], requires_approval=requires_approval,
            approval_details=approval_details,
            policy_check=policy_check if policy_check is not None else PolicyGuardrailEngine.validate_schema_compliance,
            failure_policy=failure_policy, replan_action=replan_action,
            max_retries=max_retries, max_replans=max_replans,
            may_revise_output=may_revise_output
        )
        return task_id

    def _log_lineage(self, task: Task, event: str, details: Optional[str] = None):
        entry = {"timestamp": time.time(), "run_id": self.run_id, "scenario": self.scenario_name, "task_id": task.id, "task_name": task.name, "status": task.status, "event": event, "details": details}
        with self._state_lock:
            self.audit_lineage.append(entry)
        with _RUN_HISTORY_LOCK:
            with open(self.audit_file, "a", encoding="utf-8") as audit_file:
                audit_file.write(json.dumps(entry, default=str) + "\n")
                audit_file.flush()
        logger.info(f"[{self.scenario_name}] Task '{task.name}' ({task.id}) -> {event}: {details or ''}")
        self._persist_state()

    def _persist_state(self):
        with self._state_lock:
            state_data = {
                "run_id": self.run_id,
                "scenario_name": self.scenario_name,
                "persistence_mode": "audit_only",
                "context": self.context,
                "metrics": {
                    "retries": self.total_retries,
                    "rollbacks": self.total_rollbacks,
                    "latency_sec": round(self._elapsed_seconds(), 4),
                    "success": self.success,
                    "failure_timestamp": self.failure_timestamp,
                    "recovery_timestamp": self.recovery_timestamp,
                    "mttr_sec": self._mttr_seconds(),
                },
                "tasks": {
                    tid: {key: value for key, value in asdict(task).items()
                          if key not in ["action", "compensate_action", "replan_action"]}
                    for tid, task in self.tasks.items()
                },
                "audit_lineage": self.audit_lineage
            }
            temporary_path = self.state_file + ".tmp"
            with open(temporary_path, "w", encoding="utf-8") as state_file:
                json.dump(state_data, state_file, indent=4, default=str)
            os.replace(temporary_path, self.state_file)

    def _elapsed_seconds(self) -> float:
        end_time = self._run_end_perf if self._run_end_perf is not None else time.perf_counter()
        return max(0.0, end_time - self._run_start_perf)

    def _mttr_seconds(self) -> Optional[float]:
        if self._failure_perf is None or self._recovery_perf is None:
            return None
        return round(max(0.0, self._recovery_perf - self._failure_perf), 4)

    def _mark_failure(self):
        with self._state_lock:
            if self.failure_timestamp is None:
                self.failure_timestamp = time.time()
                self._failure_perf = time.perf_counter()

    def _read_run_history(self) -> List[Dict[str, Any]]:
        with _RUN_HISTORY_LOCK:
            if not os.path.isfile(self.history_file):
                return []
            with open(self.history_file, encoding="utf-8") as history_file:
                history = json.load(history_file)
            if not isinstance(history, dict) or not isinstance(history.get("runs"), list):
                raise ValueError(f"Invalid run history format in {self.history_file}.")
            return history["runs"]

    def _append_run_history(self, record: Dict[str, Any]):
        with _RUN_HISTORY_LOCK:
            if os.path.isfile(self.history_file):
                with open(self.history_file, encoding="utf-8") as history_file:
                    history = json.load(history_file)
                if not isinstance(history, dict) or not isinstance(history.get("runs"), list):
                    raise ValueError(f"Invalid run history format in {self.history_file}.")
            else:
                history = {"runs": []}
            history["runs"].append(record)
            temporary_path = self.history_file + ".tmp"
            with open(temporary_path, "w", encoding="utf-8") as history_file:
                json.dump(history, history_file, indent=2)
            os.replace(temporary_path, self.history_file)

    def _finish_run(self, success: bool) -> bool:
        if self._run_recorded:
            return success
        self.end_time = time.time()
        self._run_end_perf = time.perf_counter()
        self.success = success
        if not success:
            self._mark_failure()
        self.recovery_timestamp = self.end_time if success and self.failure_timestamp is not None else None
        self._recovery_perf = self._run_end_perf if success and self._failure_perf is not None else None
        record = {
            "run_id": self.run_id,
            "scenario": self.scenario_name,
            "start_timestamp": self.start_time,
            "end_timestamp": self.end_time,
            "latency_sec": round(self._elapsed_seconds(), 4),
            "success": success,
            "retries": self.total_retries,
            "rollbacks": self.total_rollbacks,
            "failure_timestamp": self.failure_timestamp,
            "recovery_timestamp": self.recovery_timestamp,
            "mttr_sec": self._mttr_seconds(),
        }
        self._append_run_history(record)
        self._run_recorded = True
        self._persist_state()
        return success

    def dynamic_replan(self, failed_task: Task, error_msg: str) -> str:
        logger.warning(f"[{self.scenario_name}] DYNAMIC RE-PLANNING TRIGGERED: Upstream shift detected in '{failed_task.name}': {error_msg}")

        previous_dependencies = list(failed_task.dependencies)
        adaptive_task_id = self.add_task(
            name=f"Adaptive Re-planning Mitigation for {failed_task.name}",
            action=lambda ctx: failed_task.replan_action(ctx, error_msg),
            dependencies=previous_dependencies,
            policy_check=failed_task.policy_check
        )

        failed_task.dependencies = [adaptive_task_id]
        failed_task.retry_count = 0
        failed_task.replan_count += 1
        failed_task.status = TaskStatus.PENDING.value
        failed_task.output = None
        failed_task.error = error_msg
        return adaptive_task_id

    def _apply_output_revision(
        self,
        revision_task: Task,
        revision: TaskOutputRevision,
        completed_tasks: Set[str],
    ):
        if not revision_task.may_revise_output or not revision_task.requires_approval:
            raise ValueError("Output-revision tasks must declare the capability and require human approval.")
        if not isinstance(revision.reason, str) or not revision.reason.strip():
            raise ValueError("Output revisions require a non-empty decision reason.")
        revised_task = self.tasks.get(revision.task_id)
        if revised_task is None or revised_task.id not in completed_tasks:
            raise ValueError("Output revisions must target a completed task in this workflow.")
        if revised_task.revision_count >= 1:
            raise ValueError(f"Task '{revised_task.name}' exceeded its output revision limit.")
        if revision_task.id == revised_task.id:
            raise ValueError("A task cannot revise its own output.")

        ancestors = set(revision_task.dependencies)
        changed = True
        while changed:
            changed = False
            for task_id in tuple(ancestors):
                for dependency in self.tasks[task_id].dependencies:
                    if dependency not in ancestors:
                        ancestors.add(dependency)
                        changed = True
        if revised_task.id not in ancestors:
            raise ValueError("Revision tasks must depend on the task whose output they revise.")

        descendants = set()
        changed = True
        while changed:
            changed = False
            for task in self.tasks.values():
                if task.id not in descendants and any(
                    dependency == revised_task.id or dependency in descendants
                    for dependency in task.dependencies
                ):
                    descendants.add(task.id)
                    changed = True
        descendants.discard(revision_task.id)
        completed_descendants = descendants.intersection(completed_tasks)

        if any(self.tasks[task_id].compensate_action is None for task_id in completed_descendants):
            raise ValueError("Cannot revise upstream output: a completed dependent has no compensation action.")

        remaining = set(completed_descendants)
        ordered_descendants = []
        while remaining:
            ready = {
                task_id for task_id in remaining
                if not (set(self.tasks[task_id].dependencies) & remaining)
            }
            if not ready:
                raise ValueError("Cannot revise upstream output: dependent task graph contains a cycle.")
            ordered_descendants.extend(task_id for task_id in self.tasks if task_id in ready)
            remaining.difference_update(ready)

        if ordered_descendants:
            self.total_rollbacks += 1
        for task_id in reversed(ordered_descendants):
            task = self.tasks[task_id]
            try:
                task.compensate_action(self.context)
            except Exception as error:
                self._log_lineage(task, "REPLAN_COMPENSATION_FAILED", str(error))
                raise RuntimeError(f"Could not compensate stale dependent '{task.name}'.") from error
            task.status = TaskStatus.ROLLED_BACK.value
            completed_tasks.discard(task_id)
            self._log_lineage(task, "REPLAN_COMPENSATED", "Compensated before upstream output revision.")

        revised_task.output = revision.output
        revised_task.revision_count += 1
        revised_task.error = None
        self._log_lineage(
            revised_task,
            "OUTPUT_REVISED",
            f"Revision {revised_task.revision_count}: {revision.reason}",
        )

        for task_id in descendants - {revision_task.id}:
            task = self.tasks[task_id]
            if task_id in completed_descendants:
                task.status = TaskStatus.PENDING.value
                task.retry_count = 0
                task.output = None
                task.error = None
                task.input_context = {}
            elif task.status in (TaskStatus.FAILED.value, TaskStatus.ROLLED_BACK.value):
                task.status = TaskStatus.PENDING.value
                task.retry_count = 0
                task.output = None
                task.error = None
                task.input_context = {}
            completed_tasks.discard(task_id)
            self._log_lineage(task, "REPLAN_INVALIDATED", f"Upstream task '{revised_task.name}' changed.")

    def execute(self) -> bool:
        logger.info(f"=== Starting Scenario: {self.scenario_name} ===")
        self.start_time = time.time()
        self._run_start_perf = time.perf_counter()
        self._run_end_perf = None
        self._failure_perf = None
        self._recovery_perf = None
        self.run_id = uuid.uuid4().hex
        self.success = None
        self._run_recorded = False
        self.audit_lineage = []
        self.total_retries = 0
        self.total_rollbacks = 0
        self.failure_timestamp = None
        self.recovery_timestamp = None
        deadline = time.monotonic() + self.max_execution_timeout
        self.context.set_deadline(deadline)
        completed_tasks: Set[str] = set()

        for tid, t in self.tasks.items():
            if t.status == TaskStatus.COMPLETED.value:
                completed_tasks.add(tid)

        while len(completed_tasks) < len(self.tasks):
            remaining_timeout = deadline - time.monotonic()
            
            if remaining_timeout <= 0:
                logger.critical(f"[{self.scenario_name}] SAFE-STOP TRIGGERED: Max execution timeout of {self.max_execution_timeout}s exceeded.")
                self.context.cancel()
                self._mark_failure()
                self._trigger_rollback(completed_tasks)
                return self._finish_run(False)

            ready_tasks = [
                t for t in self.tasks.values() 
                if t.status == TaskStatus.PENDING.value and all(dep in completed_tasks for dep in t.dependencies)
            ]

            if not ready_tasks and len(completed_tasks) < len(self.tasks):
                pending_left = [t for t in self.tasks.values() if t.status == TaskStatus.PENDING.value]
                if not pending_left:
                    break
                logger.error("Execution stalled: Unresolved task graph dependencies.")
                break

            authorized_tasks, halted_task, timed_out = self._authorize_ready_tasks(ready_tasks)
            if halted_task is not None:
                for task in ready_tasks:
                    if task is halted_task:
                        continue
                    if task.status not in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value):
                        task.status = TaskStatus.FAILED.value
                        task.error = "Not executed because the ready batch was halted."
                        self._log_lineage(task, "CANCELLED", task.error)
                self.context.cancel()
                self._mark_failure()
                event = "TIMEOUT_FAILURE" if timed_out else "GOVERNANCE_HALT"
                details = "Approval exceeded the workflow deadline." if timed_out else "Workflow terminated during governance preflight."
                self._log_lineage(halted_task, event, details)
                self._trigger_rollback(completed_tasks)
                return self._finish_run(False)

            executor = ThreadPoolExecutor(max_workers=4)
            future_to_task = {executor.submit(self._execute_single_task, task): task for task in authorized_tasks}
            pending_futures = set(future_to_task)
            output_revisions = []
            fatal_failure = False
            timed_out = False

            while pending_futures and not fatal_failure:
                remaining_timeout = deadline - time.monotonic()
                if remaining_timeout <= 0:
                    timed_out = True
                    break

                done, pending_futures = wait(
                    pending_futures,
                    timeout=remaining_timeout,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    timed_out = True
                    break

                recoverable_failures = []
                for future in done:
                    task = future_to_task[future]
                    try:
                        success = future.result()
                    except Exception as error:
                        task.status = TaskStatus.FAILED.value
                        task.error = str(error)
                        self._mark_failure()
                        self._log_lineage(task, "CRITICAL_FAILURE", str(error))
                        fatal_failure = True
                        continue

                    if success:
                        completed_tasks.add(task.id)
                        if isinstance(task.output, TaskOutputRevision):
                            output_revisions.append((task, task.output))
                        continue

                    self._mark_failure()
                    if task.failure_policy is FailurePolicy.REPLAN and task.replan_count < task.max_replans:
                        recoverable_failures.append(task)
                    else:
                        logger.critical(f"[{self.scenario_name}] TASK FAILURE: '{task.name}' failed after max retries. Triggering rollback.")
                        task.status = TaskStatus.FAILED.value
                        self._log_lineage(task, "EXECUTION_FAILURE", "Task failed permanently. Initiating rollback.")
                        fatal_failure = True

                if not fatal_failure:
                    for task in recoverable_failures:
                        new_mitigation_id = self.dynamic_replan(task, task.error or "Upstream execution failure")
                        self._log_lineage(task, "DYNAMIC_REPLAN_APPLIED", f"Injected mitigation task ID: {new_mitigation_id}")

            if not fatal_failure and not timed_out:
                for revision_task, revision in output_revisions:
                    try:
                        self._apply_output_revision(revision_task, revision, completed_tasks)
                    except Exception as error:
                        completed_tasks.discard(revision_task.id)
                        revision_task.status = TaskStatus.FAILED.value
                        revision_task.error = str(error)
                        self._mark_failure()
                        self._log_lineage(revision_task, "REPLAN_REJECTED", str(error))
                        fatal_failure = True
                        break

            if timed_out or fatal_failure:
                self.context.cancel()
                for future in pending_futures:
                    task = future_to_task[future]
                    future.cancel()
                    if task.status not in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value):
                        task.status = TaskStatus.FAILED.value
                        task.error = "Execution deadline exceeded" if timed_out else "Cancelled after workflow failure"
                        event = "TIMEOUT_FAILURE" if timed_out else "CANCELLED"
                        self._log_lineage(task, event, task.error)
                executor.shutdown(wait=False, cancel_futures=True)
                drained_futures, active_futures = wait(
                    pending_futures,
                    timeout=CANCELLATION_DRAIN_TIMEOUT_SEC,
                )
                for future in drained_futures:
                    if future.cancelled():
                        continue
                    task = future_to_task[future]
                    try:
                        if future.result():
                            completed_tasks.add(task.id)
                    except Exception:
                        pass
                if active_futures:
                    for future in active_futures:
                        task = future_to_task[future]
                        self._log_lineage(
                            task,
                            "SAFE_STOP_INCOMPLETE",
                            "Worker remained active after cancellation; rollback skipped to avoid racing side effects.",
                        )
                    self._mark_failure()
                    return self._finish_run(False)
                executor.shutdown(wait=True, cancel_futures=True)
                if timed_out:
                    logger.critical(f"[{self.scenario_name}] COOPERATIVE TIMEOUT: Cancellation requested after {self.max_execution_timeout}s.")
                self._mark_failure()
                self._trigger_rollback(completed_tasks)
                return self._finish_run(False)

            executor.shutdown(wait=True)

        all_success = all(t.status in [TaskStatus.COMPLETED.value] for t in self.tasks.values())
        if all_success:
            logger.info(f"=== Scenario '{self.scenario_name}' Completed & Recovered Successfully ===")
            return self._finish_run(True)
        else:
            logger.warning(f"=== Scenario '{self.scenario_name}' Finished with Unresolved Failures ===")
            return self._finish_run(False)

    def _authorize_ready_tasks(self, ready_tasks: List[Task]) -> Tuple[List[Task], Optional[Task], bool]:
        authorized_tasks = []
        for task in ready_tasks:
            try:
                self.context.check_cancelled()
            except ExecutionCancelledError as error:
                task.status = TaskStatus.FAILED.value
                task.error = str(error)
                self._log_lineage(task, "TIMEOUT_FAILURE", task.error)
                return authorized_tasks, task, True

            task.status = TaskStatus.RUNNING.value
            self._log_lineage(task, "STARTED")
            try:
                if task.policy_check and not task.policy_check(self.context):
                    task.status = TaskStatus.FAILED.value
                    task.error = "Policy Guardrail Violation"
                    self._log_lineage(task, "POLICY_BLOCKED", "Security or compliance policy check failed.")
                    return authorized_tasks, task, False
            except Exception as error:
                task.status = TaskStatus.FAILED.value
                task.error = f"Policy check failed: {error}"
                self._log_lineage(task, "POLICY_CHECK_ERROR", task.error)
                return authorized_tasks, task, False

            if task.requires_approval:
                task.status = TaskStatus.WAITING_APPROVAL.value
                approval_prompt = self._approval_prompt(task)
                self._log_lineage(task, "PENDING_APPROVAL", approval_prompt)
                remaining_timeout = max(0.0, self.context.deadline - time.monotonic())
                try:
                    approval = self._request_approval(
                        approval_prompt,
                        remaining_timeout,
                    )
                except Exception as error:
                    task.status = TaskStatus.FAILED.value
                    task.error = f"Approval provider failed: {error}"
                    self._log_lineage(task, "APPROVAL_PROVIDER_ERROR", task.error)
                    return authorized_tasks, task, False
                try:
                    self.context.check_cancelled()
                except ExecutionCancelledError as error:
                    task.status = TaskStatus.FAILED.value
                    task.error = str(error)
                    self._log_lineage(task, "TIMEOUT_FAILURE", task.error)
                    return authorized_tasks, task, True
                if approval is None:
                    task.status = TaskStatus.FAILED.value
                    task.error = "Approval deadline expired or no response was available."
                    self._log_lineage(task, "APPROVAL_TIMEOUT", task.error)
                    return authorized_tasks, task, True
                if approval.strip().lower() != "y":
                    task.status = TaskStatus.FAILED.value
                    task.error = "Operator Governance Rejection"
                    self._log_lineage(task, "REJECTED", "Operator denied governance approval.")
                    return authorized_tasks, task, False
                self._log_lineage(task, "APPROVED", "Operator approved after reviewing the recorded change summary.")

            task.status = TaskStatus.RUNNING.value
            authorized_tasks.append(task)
        return authorized_tasks, None, False

    def _approval_prompt(self, task: Task) -> str:
        details = task.approval_details or "No change summary was provided."
        prerequisite_outputs = [
            f"- {self.tasks[dependency_id].name}: {self.tasks[dependency_id].output!r}"
            for dependency_id in task.dependencies
        ]
        if prerequisite_outputs:
            details += "\nCompleted prerequisites:\n" + "\n".join(prerequisite_outputs)
        return f"\n[GOVERNANCE GATE] Review and authorize '{task.name}'.\n{details}\nApprove? (y/n): "

    def _request_approval(self, prompt: str, timeout: float) -> Optional[str]:
        if timeout <= 0:
            return None
        responses = queue.Queue(maxsize=1)

        def read_response():
            try:
                responses.put((True, self.approval_provider(prompt, timeout)))
            except Exception as error:
                responses.put((False, error))

        deadline = time.monotonic() + timeout
        reader = threading.Thread(target=read_response, name="ApprovalProvider", daemon=True)
        reader.start()
        try:
            succeeded, response = responses.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            return None
        if not succeeded:
            raise RuntimeError(str(response)) from response
        return response

    def _execute_single_task(self, task: Task) -> bool:
        self.context.check_cancelled()

        while task.retry_count < task.max_retries:
            try:
                self.context.check_cancelled()
                task.input_context = copy.deepcopy(dict(self.context))
                task.output = task.action(self.context)
                self.context.check_cancelled()
                task.status = TaskStatus.COMPLETED.value
                self._log_lineage(task, "COMPLETED", f"Output: {task.output}")
                return True
            except ExecutionCancelledError as error:
                task.status = TaskStatus.FAILED.value
                task.error = str(error)
                self._log_lineage(task, "CANCELLED", task.error)
                return False
            except Exception as e:
                task.retry_count += 1
                self.total_retries += 1
                task.error = str(e)
                self._mark_failure()
                self._log_lineage(task, "RETRY", f"Attempt {task.retry_count}/{task.max_retries} failed: {e}")
                if self.context.cancel_event.wait(0.5):
                    task.status = TaskStatus.FAILED.value
                    self._log_lineage(task, "CANCELLED", "Retry backoff interrupted by cancellation.")
                    return False

        task.status = TaskStatus.FAILED.value
        self._log_lineage(task, "MAX_RETRIES_EXHAUSTED")
        return False

    def _trigger_rollback(self, completed_task_ids: Set[str]):
        logger.warning(f"[{self.scenario_name}] Triggering Compensation Rollback across completed nodes.")
        self.total_rollbacks += 1
        remaining = set(completed_task_ids)
        rollback_order = []
        while remaining:
            ready = [
                task_id for task_id in self.tasks
                if task_id in remaining
                and not any(
                    task_id in self.tasks[dependent_id].dependencies
                    for dependent_id in remaining
                )
            ]
            if not ready:
                logger.error("Rollback stopped because completed tasks contain a dependency cycle.")
                return
            rollback_order.extend(ready)
            remaining.difference_update(ready)

        for tid in rollback_order:
            task = self.tasks[tid]
            if task.compensate_action:
                try:
                    task.compensate_action(self.context)
                    task.status = TaskStatus.ROLLED_BACK.value
                    self._log_lineage(task, "ROLLED_BACK", "Compensating action successfully executed.")
                except Exception as ce:
                    self._log_lineage(task, "ROLLBACK_FAILED", str(ce))

    def get_metrics(self) -> Dict[str, Any]:
        duration = self._elapsed_seconds()
        scenario_runs = [
            record for record in self._read_run_history()
            if record.get("scenario") == self.scenario_name
        ]
        successful_runs = sum(1 for record in scenario_runs if record.get("success") is True)
        success_rate = (successful_runs / len(scenario_runs) * 100.0) if scenario_runs else 0.0
        return {
            "run_id": self.run_id,
            "scenario": self.scenario_name,
            "end_to_end_latency_sec": round(duration, 4),
            "total_retries": self.total_retries,
            "total_rollbacks": self.total_rollbacks,
            "mttr_sec": self._mttr_seconds(),
            "failure_timestamp": self.failure_timestamp,
            "recovery_timestamp": self.recovery_timestamp,
            "success": bool(self.success),
            "recorded_runs": len(scenario_runs),
            "successful_runs": successful_runs,
            "success_rate_percentage": round(success_rate, 2),
        }
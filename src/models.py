from dataclasses import dataclass, field
from enum import Enum
import threading
import time
from typing import List, Dict, Any, Callable, Optional

class TaskStatus(Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class FailurePolicy(Enum):
    FATAL = "FATAL"
    REPLAN = "REPLAN"


class ExecutionCancelledError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskOutputRevision:
    task_id: str
    output: Any
    reason: str


class ExecutionContext(dict):
    def __init__(self):
        super().__init__()
        self.cancel_event = threading.Event()
        self.deadline: Optional[float] = None

    def set_deadline(self, deadline: float):
        self.deadline = deadline
        self.cancel_event.clear()

    def cancel(self):
        self.cancel_event.set()

    def check_cancelled(self):
        if self.cancel_event.is_set() or (
            self.deadline is not None and time.monotonic() >= self.deadline
        ):
            self.cancel_event.set()
            raise ExecutionCancelledError("Workflow execution was cancelled or timed out.")


@dataclass
class Task:
    id: str
    name: str
    action: Callable[..., Any]
    compensate_action: Optional[Callable[..., Any]] = None
    dependencies: List[str] = field(default_factory=list)
    requires_approval: bool = False
    policy_check: Optional[Callable[..., bool]] = None
    failure_policy: FailurePolicy = FailurePolicy.FATAL
    replan_action: Optional[Callable[..., Any]] = None
    max_retries: int = 3
    max_replans: int = 1
    status: str = TaskStatus.PENDING.value
    retry_count: int = 0
    replan_count: int = 0
    revision_count: int = 0
    may_revise_output: bool = False
    input_context: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    output: Any = None
    approval_details: Optional[str] = None
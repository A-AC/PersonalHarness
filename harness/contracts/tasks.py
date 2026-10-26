from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class TaskSpec:
    goal: str
    domain_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    interaction_id: str | None = None
    # Higher priority runs first; 0 is the default ordering (sequence/created).
    priority: int = 0
    # Optional UTC ISO deadline. Purely informational until it passes, at which
    # point the scheduler surfaces the task as due.
    due_at: str | None = None
    # The durable plan decision this step came from, when known. A plan is a
    # proposal; only execution attempts and outcomes prove progress.
    plan_decision_id: str | None = None


@dataclass
class TaskOutcome:
    summary: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    id: str
    goal: str
    state: str
    created_at: str
    updated_at: str
    domain_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    block_reason: str | None = None
    outcome: TaskOutcome | None = None
    interaction_id: str | None = None
    sequence: int = 0
    delivered_response: bool = False
    succeeded_execution: bool = False
    last_message: str | None = None
    priority: int = 0
    due_at: str | None = None
    # Eligibility time while deferred. Cleared on any transition out of
    # DEFERRED so a stale window never resurrects old work.
    defer_until: str | None = None
    # Concise persisted progress summaries (checkpoints). Attempt-level
    # evidence stays in the capability attempt store; this is the readable
    # progress trail. Bounded by the task manager.
    progress: list[dict[str, Any]] = field(default_factory=list)
    # How many times the scheduler has requeued this task after it stopped
    # before completion (interruption or recoverable failure). Bounded by
    # scheduler.max_task_retries.
    resume_count: int = 0
    plan_decision_id: str | None = None


class TaskManager(Protocol):
    def create(self, spec: TaskSpec) -> Task: ...
    def get(self, task_id: str) -> Task: ...
    def list(self, *, states: list[str] | None = None) -> list[Task]: ...
    def transition(self, task_id: str, state: str, *, reason: str | None = None) -> Task: ...
    def record_outcome(self, task_id: str, outcome: TaskOutcome) -> None: ...
    def append_constraint(self, task_id: str, text: str) -> Task: ...
    def mark_delivered(self, task_id: str, message: str) -> Task: ...
    def mark_succeeded_execution(self, task_id: str) -> Task: ...
    def assign_domain(self, task_id: str, domain_id: str | None) -> Task: ...
    def next_ready(self) -> Task | None: ...
    def on_process_start(self) -> list[Task]: ...
    # Postpone the task until ``until`` (UTC ISO). The decision-level "later"
    # reply and scheduler backoff both land here; releasing the window is a
    # transition back to READY that only the scheduler performs.
    def defer(self, task_id: str, until: str | None, *, reason: str | None = None) -> Task: ...
    # Requeue stopped work (interruption, transient block, or recoverable
    # failure) as READY and count the resume against the retry budget. The
    # caller is responsible for checking attempt evidence first; this method
    # never inspects evidence itself.
    def resume(self, task_id: str, *, reason: str | None = None) -> Task: ...
    # Persist one concise progress summary on the task.
    def record_checkpoint(self, task_id: str, summary: str, data: dict[str, Any] | None = None) -> Task: ...
    def set_priority(self, task_id: str, priority: int) -> Task: ...
    def set_due(self, task_id: str, due_at: str | None) -> Task: ...

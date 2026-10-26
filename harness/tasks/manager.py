from __future__ import annotations

import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from harness.constants import BlockReason, TaskState
from harness.contracts.events import Event, EventLog
from harness.contracts.settings import SettingsService
from harness.contracts.tasks import Task, TaskOutcome, TaskSpec
from harness.storage.atomic import atomic_write_json, new_id, now_ts, read_json


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    TaskState.PENDING: {
        TaskState.READY,
        TaskState.BLOCKED,
        TaskState.DEFERRED,
        TaskState.CANCELLED,
    },
    TaskState.READY: {
        TaskState.RUNNING,
        TaskState.BLOCKED,
        TaskState.DEFERRED,
        TaskState.CANCELLED,
    },
    TaskState.RUNNING: {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.BLOCKED,
        TaskState.CANCELLED,
    },
    TaskState.BLOCKED: {
        TaskState.READY,
        TaskState.RUNNING,
        TaskState.DEFERRED,
        TaskState.FAILED,
        TaskState.COMPLETED,
        # A reason-only change (for example awaiting_user -> unknown_outcome)
        # is the same blocked state; the event records the new reason.
        TaskState.BLOCKED,
        TaskState.CANCELLED,
    },
    TaskState.DEFERRED: {
        TaskState.READY,
        TaskState.RUNNING,
        TaskState.BLOCKED,
        TaskState.FAILED,
        # Re-deferring (extending the window) is the same state.
        TaskState.DEFERRED,
        TaskState.CANCELLED,
    },
    TaskState.COMPLETED: set(),
    # FAILED is terminal for the model's action machine, but the scheduler may
    # requeue a *retryable* failure (evidence-checked) or the user may defer
    # or cancel it. Nothing else can revive failed work.
    TaskState.FAILED: {
        TaskState.READY,
        TaskState.DEFERRED,
        TaskState.CANCELLED,
    },
    TaskState.CANCELLED: set(),
}

# How many persisted progress checkpoints a task keeps (oldest dropped).
PROGRESS_LIMIT = 20


class FileTaskManager:
    def __init__(self, directory: Path, settings: SettingsService, events: EventLog):
        self._dir = directory
        self._settings = settings
        self._events = events
        directory.mkdir(parents=True, exist_ok=True)

    def _rw(self) -> tuple[int, float]:
        return int(self._settings.get("storage.atomic_retries")), float(self._settings.get("storage.atomic_retry_s"))

    def _path(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.json"

    def _save(self, task: Task) -> None:
        retries, retry_s = self._rw()
        data = asdict(task)
        atomic_write_json(self._path(task.id), data, retries=retries, retry_s=retry_s)

    def _load(self, task_id: str) -> Task:
        path = self._path(task_id)
        if not path.exists():
            raise KeyError(task_id)
        data = read_json(path)
        outcome = data.get("outcome")
        if outcome:
            data["outcome"] = TaskOutcome(**outcome)
        # Legacy task rows may carry structure keys that this release removed.
        # Keep the row loadable by passing only fields the contract still has.
        known = {item.name for item in fields(Task)}
        data = {key: value for key, value in data.items() if key in known}
        return Task(**data)

    def create(self, spec: TaskSpec) -> Task:
        ts = now_ts()
        state = TaskState.READY if not spec.depends_on else TaskState.PENDING
        reason = BlockReason.DEPENDENCY if spec.depends_on else None
        if spec.depends_on:
            if all(self.get(dep).state == TaskState.COMPLETED for dep in spec.depends_on):
                state = TaskState.READY
                reason = None
            else:
                state = TaskState.BLOCKED
                reason = BlockReason.DEPENDENCY
        task = Task(
            id=new_id("tsk"),
            goal=spec.goal,
            state=state,
            created_at=ts,
            updated_at=ts,
            domain_id=spec.domain_id,
            depends_on=list(spec.depends_on),
            constraints=list(spec.constraints),
            block_reason=reason,
            interaction_id=spec.interaction_id,
            sequence=time.time_ns(),
            priority=int(spec.priority or 0),
            due_at=spec.due_at,
            plan_decision_id=spec.plan_decision_id,
        )
        self._save(task)
        self._events.append(
            Event(id=new_id("evt"), ts=ts, actor="harness", operation="task.create", target=task.id, result=task.state)
        )
        return task

    def get(self, task_id: str) -> Task:
        return self._load(task_id)

    def list(self, *, states: list[str] | None = None) -> list[Task]:
        items = [self._load(path.stem) for path in sorted(self._dir.glob("tsk_*.json"))]
        if states is not None:
            items = [item for item in items if item.state in states]
        return items

    def transition(self, task_id: str, state: str, *, reason: str | None = None) -> Task:
        task = self._load(task_id)
        allowed = ALLOWED_TRANSITIONS.get(task.state)
        if allowed is None:
            raise ValueError(f"unknown task state: {task.state}")
        if state not in allowed:
            raise ValueError(f"invalid task transition: {task.state} -> {state}")
        reason_only = state == task.state == TaskState.BLOCKED
        if not reason_only:
            task.state = state
        task.block_reason = reason
        if state != TaskState.DEFERRED:
            task.defer_until = None
        task.updated_at = now_ts()
        self._save(task)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=task.updated_at,
                actor="harness",
                operation="task.transition",
                target=task.id,
                result=state,
                data={"reason": reason},
            )
        )
        if state == TaskState.COMPLETED and not reason_only:
            self._release_dependents(task.id)
        return task

    def _release_dependents(self, completed_id: str) -> None:
        for task in self.list():
            if completed_id in task.depends_on and task.state in {TaskState.PENDING, TaskState.BLOCKED}:
                remaining = [dep for dep in task.depends_on if self.get(dep).state != TaskState.COMPLETED]
                if not remaining:
                    self.transition(task.id, TaskState.READY, reason=None)

    def record_outcome(self, task_id: str, outcome: TaskOutcome) -> None:
        task = self._load(task_id)
        task.outcome = outcome
        task.updated_at = now_ts()
        self._save(task)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=task.updated_at,
                actor="harness",
                operation="task.outcome",
                target=task.id,
                result=outcome.status,
                data={"summary": outcome.summary},
            )
        )

    def append_constraint(self, task_id: str, text: str) -> Task:
        task = self._load(task_id)
        task.constraints.append(text)
        task.updated_at = now_ts()
        self._save(task)
        return task

    def mark_delivered(self, task_id: str, message: str) -> Task:
        task = self._load(task_id)
        task.delivered_response = True
        task.last_message = message
        task.updated_at = now_ts()
        self._save(task)
        return task

    def mark_succeeded_execution(self, task_id: str) -> Task:
        task = self._load(task_id)
        task.succeeded_execution = True
        task.updated_at = now_ts()
        self._save(task)
        return task

    def assign_domain(self, task_id: str, domain_id: str | None) -> Task:
        task = self._load(task_id)
        task.domain_id = domain_id
        task.updated_at = now_ts()
        self._save(task)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=task.updated_at,
                actor="harness",
                operation="task.domain",
                target=task.id,
                result="ok",
                data={"domain_id": domain_id},
            )
        )
        return task

    def defer(self, task_id: str, until: str | None, *, reason: str | None = None) -> Task:
        task = self._load(task_id)
        blocked_reason = reason or BlockReason.DEFERRED.value
        task = self.transition(task_id, TaskState.DEFERRED.value, reason=blocked_reason)
        # transition() clears defer_until for non-deferred targets only; set
        # the window explicitly (None means an indefinite deferral).
        task.defer_until = until
        task.updated_at = now_ts()
        self._save(task)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=task.updated_at,
                actor="harness",
                operation="task.defer",
                target=task.id,
                result=TaskState.DEFERRED.value,
                data={"until": until, "reason": blocked_reason},
            )
        )
        return task

    def resume(self, task_id: str, *, reason: str | None = None) -> Task:
        """Requeue stopped work as READY, counting against the retry budget.

        Only the scheduler (evidence-checked) and the control plane (explicit
        user instruction) call this; the model's action machine never revives
        stopped work on its own.
        """
        task = self._load(task_id)
        task.resume_count += 1
        task.updated_at = now_ts()
        self._save(task)
        resumed = self.transition(task_id, TaskState.READY.value, reason=None)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=resumed.updated_at,
                actor="harness",
                operation="task.resume",
                target=task_id,
                result=TaskState.READY.value,
                data={"reason": reason, "resume_count": task.resume_count},
            )
        )
        return resumed

    def record_checkpoint(self, task_id: str, summary: str, data: dict[str, Any] | None = None) -> Task:
        task = self._load(task_id)
        entry: dict[str, Any] = {"ts": now_ts(), "summary": str(summary)}
        if data:
            entry["data"] = dict(data)
        task.progress.append(entry)
        if len(task.progress) > PROGRESS_LIMIT:
            task.progress = task.progress[-PROGRESS_LIMIT:]
        task.updated_at = now_ts()
        self._save(task)
        return task

    def set_priority(self, task_id: str, priority: int) -> Task:
        task = self._load(task_id)
        task.priority = int(priority)
        task.updated_at = now_ts()
        self._save(task)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=task.updated_at,
                actor="harness",
                operation="task.update",
                target=task.id,
                result="priority",
                data={"priority": task.priority},
            )
        )
        return task

    def set_due(self, task_id: str, due_at: str | None) -> Task:
        task = self._load(task_id)
        task.due_at = due_at
        task.updated_at = now_ts()
        self._save(task)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=task.updated_at,
                actor="harness",
                operation="task.update",
                target=task.id,
                result="due_at",
                data={"due_at": due_at},
            )
        )
        return task

    def next_ready(self) -> Task | None:
        ready = self.list(states=[TaskState.READY])
        if not ready:
            return None
        # Priority first (higher wins), then creation order for stability.
        return sorted(ready, key=lambda item: (-item.priority, item.sequence, item.created_at))[0]

    def on_process_start(self) -> list[Task]:
        interrupted = []
        for task in self.list(states=[TaskState.RUNNING]):
            interrupted.append(self.transition(task.id, TaskState.BLOCKED, reason=BlockReason.INTERRUPTED))
        return interrupted

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path

from harness.constants import BlockReason, TaskState
from harness.contracts.events import Event, EventLog
from harness.contracts.settings import SettingsService
from harness.contracts.tasks import Task, TaskOutcome, TaskSpec
from harness.storage.atomic import atomic_write_json, new_id, now_ts, read_json


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    TaskState.PENDING: {TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.READY: {TaskState.RUNNING, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.RUNNING: {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.BLOCKED,
        TaskState.CANCELLED,
    },
    TaskState.BLOCKED: {TaskState.READY, TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.COMPLETED: set(),
    TaskState.FAILED: set(),
    TaskState.CANCELLED: set(),
}


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
            project_id=spec.project_id,
            workspace_id=spec.workspace_id,
            depends_on=list(spec.depends_on),
            constraints=list(spec.constraints),
            block_reason=reason,
            interaction_id=spec.interaction_id,
            sequence=time.time_ns(),
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
        task.state = state
        task.block_reason = reason
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
        if state == TaskState.COMPLETED:
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

    def assign_structure(
        self,
        task_id: str,
        *,
        domain_id: str | None,
        project_id: str | None,
        workspace_id: str | None,
    ) -> Task:
        task = self._load(task_id)
        task.domain_id = domain_id
        task.project_id = project_id
        task.workspace_id = workspace_id
        task.updated_at = now_ts()
        self._save(task)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=task.updated_at,
                actor="harness",
                operation="task.structure",
                target=task.id,
                result="ok",
                data={
                    "domain_id": domain_id,
                    "project_id": project_id,
                    "workspace_id": workspace_id,
                },
            )
        )
        return task

    def next_ready(self) -> Task | None:
        ready = self.list(states=[TaskState.READY])
        if not ready:
            return None
        return sorted(ready, key=lambda item: (item.sequence, item.created_at))[0]

    def on_process_start(self) -> list[Task]:
        interrupted = []
        for task in self.list(states=[TaskState.RUNNING]):
            interrupted.append(self.transition(task.id, TaskState.BLOCKED, reason=BlockReason.INTERRUPTED))
        return interrupted

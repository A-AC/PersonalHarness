from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class TaskSpec:
    goal: str
    domain_id: str | None = None
    project_id: str | None = None
    workspace_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    interaction_id: str | None = None


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
    project_id: str | None = None
    workspace_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    block_reason: str | None = None
    outcome: TaskOutcome | None = None
    interaction_id: str | None = None
    sequence: int = 0
    delivered_response: bool = False
    succeeded_execution: bool = False
    last_message: str | None = None


class TaskManager(Protocol):
    def create(self, spec: TaskSpec) -> Task: ...
    def get(self, task_id: str) -> Task: ...
    def list(self, *, states: list[str] | None = None) -> list[Task]: ...
    def transition(self, task_id: str, state: str, *, reason: str | None = None) -> Task: ...
    def record_outcome(self, task_id: str, outcome: TaskOutcome) -> None: ...
    def append_constraint(self, task_id: str, text: str) -> Task: ...
    def mark_delivered(self, task_id: str, message: str) -> Task: ...
    def mark_succeeded_execution(self, task_id: str) -> Task: ...
    def next_ready(self) -> Task | None: ...
    def on_process_start(self) -> list[Task]: ...

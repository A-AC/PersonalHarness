from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from harness.contracts.actions import ActAction, ExecutionResult
from harness.contracts.decisions import Decision
from harness.contracts.tasks import Task


@dataclass
class ApprovedAction:
    """One action the user approved for a single dispatch.

    The Control Plane returns this to the action loop. It never executes the
    action itself and never sets ``permission_override``.
    """

    decision_id: str
    task_id: str | None
    proposal: dict[str, Any]


@dataclass
class ActionGate:
    """Outcome of the pre-dispatch confirmation check."""

    allowed: bool
    decision: Decision | None = None
    prompt: str | None = None

    @property
    def pending(self) -> bool:
        return not self.allowed


@dataclass
class ControlDirective:
    """The Control Plane's decision for the current turn."""

    # plan: create tasks for new work; resume: answer a blocked question;
    # execute: dispatch one or more approved actions; handled: already answered.
    mode: str
    intent: str
    messages: list[str] = field(default_factory=list)
    decision_ids: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    actions: list[ApprovedAction] = field(default_factory=list)
    reason: str = ""

    @property
    def handled(self) -> bool:
        return self.mode == "handled"


class ControlPlane(Protocol):
    def begin_turn(self, user_text: str, interaction_id: str) -> ControlDirective: ...

    def gate_action(
        self,
        proposal: ActAction,
        task: Task,
        interaction_id: str,
    ) -> ActionGate: ...

    def record_execution(
        self,
        decision_id: str,
        execution: ExecutionResult,
        *,
        task_id: str | None = None,
        final: bool = True,
    ) -> Decision | None: ...

    def propose_action(
        self,
        task_id: str,
        capability: str,
        operation: str,
        arguments: dict[str, Any] | None = None,
        *,
        interaction_id: str | None = None,
        prompt: str | None = None,
        risk: str | None = None,
    ) -> Decision: ...

    def propose_plan(
        self,
        steps: list[dict[str, Any]],
        *,
        task_id: str | None = None,
        interaction_id: str | None = None,
        prompt: str | None = None,
        risk: str | None = None,
    ) -> Decision: ...

    def pending_decisions(self) -> list[Decision]: ...

    def on_start(self) -> None: ...

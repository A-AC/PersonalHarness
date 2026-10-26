from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from harness.contracts.domains import Suggestion


@dataclass
class Decision:
    """One durable proposal awaiting an explicit user decision.

    A decision describes *what* was proposed, not an authorization to do it.
    Approval is intentionally one-time (``authorization="one_time"``). Existing
    capability manifests, policy settings, domain enablement, argument
    validation, and execution guards still run when an approved action is
    dispatched.
    """

    id: str
    kind: str
    status: str
    created_at: str
    updated_at: str
    prompt: str
    summary: str
    proposal: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    risk: str = "unknown"
    task_id: str | None = None
    interaction_id: str | None = None
    domain_id: str | None = None
    source: str = "harness"
    authorization: str = "one_time"
    resolution: str | None = None
    resolution_text: str | None = None
    resolved_at: str | None = None
    resolution_interaction_id: str | None = None
    consumed_at: str | None = None
    operation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status in {"pending", "deferred"}

    @property
    def task_ids(self) -> list[str]:
        values = self.scope.get("task_ids") if isinstance(self.scope, dict) else None
        if isinstance(values, list):
            return [str(item) for item in values]
        return [self.task_id] if self.task_id else []


# A descriptive alias for callers that prefer the "pending" framing.
PendingDecision = Decision


class DecisionStore(Protocol):
    def create(self, decision: Decision) -> Decision: ...

    def get(self, decision_id: str) -> Decision: ...

    def list(self, status: str | None = None, *, include_resolved: bool = True) -> list[Decision]: ...

    def pending(self, *, include_deferred: bool = True) -> list[Decision]: ...

    def update(self, decision: Decision) -> Decision: ...

    def find_for_task(self, task_id: str, *, open_only: bool = False) -> Decision | None: ...

    def find_for_suggestion(self, suggestion_id: str) -> Decision | None: ...

    def sync_suggestion(self, suggestion: Suggestion) -> Decision | None: ...


class DecisionResolver(Protocol):
    def interpret(
        self,
        text: str,
        *,
        decisions: list[Decision],
        awaiting_question: bool = False,
    ) -> "DecisionInterpretation": ...


@dataclass
class DecisionInterpretation:
    """Deterministic reading of a user reply in the current decision context."""

    intent: str
    decision_ids: list[str] = field(default_factory=list)
    selected_id: str | None = None
    ambiguous: bool = False
    modification: dict[str, Any] | None = None
    reason: str = ""

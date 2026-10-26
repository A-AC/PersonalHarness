from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from harness.contracts.tasks import Task


@dataclass
class TurnEvent:
    """One item on the turn event channel (SSE or another transport)."""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)


class TurnListener(Protocol):
    def __call__(self, event: TurnEvent) -> None: ...


@dataclass
class TurnResult:
    interaction_id: str
    messages: list[str]
    tasks: list[Task] = field(default_factory=list)
    error: str | None = None


class InteractionLoop(Protocol):
    def handle_turn(
        self,
        user_text: str,
        *,
        emit: TurnListener | None = None,
    ) -> TurnResult: ...

    def run_ready(
        self,
        *,
        limit: int,
        emit: TurnListener | None = None,
    ) -> TurnResult: ...

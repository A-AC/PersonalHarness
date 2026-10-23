from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from harness.contracts.tasks import Task


@dataclass
class TurnResult:
    interaction_id: str
    messages: list[str]
    tasks: list[Task] = field(default_factory=list)
    error: str | None = None


class InteractionLoop(Protocol):
    def handle_turn(self, user_text: str) -> TurnResult: ...

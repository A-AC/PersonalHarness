from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from harness.contracts.loop import TurnEvent, TurnListener, TurnResult
from harness.contracts.traces import Trace


@dataclass
class MemoryUse:
    id: str
    type: str
    scope: str
    content: str
    reason: str


@dataclass
class MemoryWrite:
    memory_id: str
    operation: str
    status: str
    message: str
    restore_ids: list[str] = field(default_factory=list)
    undo: str = "retract"


@dataclass
class TurnDetail:
    interaction_id: str
    traces: list[Trace] = field(default_factory=list)
    memories: list[MemoryUse] = field(default_factory=list)
    writes: list[MemoryWrite] = field(default_factory=list)
    error: str | None = None

    @property
    def trace_ids(self) -> list[str]:
        return [item.id for item in self.traces]


class TurnService(Protocol):
    """The one entry point used by the terminal and the web UI.

    ``submit`` runs a full user turn and reports progress to an optional
    listener. ``run_ready`` is the scheduler's entry point: it runs
    already-eligible tasks through the same serialized, gated path. Both
    clients use these methods; neither re-implements turn orchestration.
    """

    def submit(self, user_text: str, listener: TurnListener | None = None) -> TurnResult: ...

    def run_ready(self, *, limit: int, listener: TurnListener | None = None) -> TurnResult: ...

    def detail(self, interaction_id: str) -> TurnDetail: ...

    def detail_many(self, interaction_ids: list[str]) -> dict[str, TurnDetail]: ...

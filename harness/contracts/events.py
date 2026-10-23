from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Event:
    id: str
    ts: str
    actor: str
    operation: str
    target: str
    result: str
    trigger_ref: str | None = None
    refs: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


class EventLog(Protocol):
    def append(self, event: Event) -> Event: ...
    def query(
        self,
        *,
        actor: str | None = None,
        operation: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> list[Event]: ...

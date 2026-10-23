from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Trace:
    id: str
    ts: str
    interaction_id: str
    task_id: str
    role: str
    messages: list[dict[str, Any]]
    included: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    raw_output: str | None = None
    parsed: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    retries: int = 0
    latency_ms: int | None = None
    token_counts: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None


class TraceStore(Protocol):
    def put(self, trace: Trace) -> None: ...
    def get(self, trace_id: str) -> Trace | None: ...
    def list_for(
        self,
        *,
        interaction_id: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[Trace]: ...

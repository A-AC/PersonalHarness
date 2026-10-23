from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ConsolidationRun:
    id: str
    started_at: str
    ended_at: str | None
    trigger: str
    status: str
    input_snapshot_ref: str
    proposals: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    rolled_back: bool = False


class ConsolidationService(Protocol):
    def run(self, *, trigger: str) -> ConsolidationRun: ...
    def get(self, run_id: str) -> ConsolidationRun: ...
    def list(self, limit: int = 20) -> list[ConsolidationRun]: ...
    def rollback(self, run_id: str, actor: str) -> ConsolidationRun: ...

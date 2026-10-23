from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class MemoryRecord:
    id: str
    type: str
    content: str
    scope: str
    origin: str
    status: str
    created_at: str
    updated_at: str
    provenance: dict[str, Any]
    supersedes: str | None = None
    superseded_by: str | None = None
    run_id: str | None = None


@dataclass
class MemoryCandidate:
    type: str
    content: str
    scope: str
    origin: str
    provenance: dict[str, Any]
    supersedes: str | None = None
    run_id: str | None = None


@dataclass
class RankedMemory:
    record: MemoryRecord
    score: float
    reason: str


@dataclass
class SubmitResult:
    accepted: bool
    record: MemoryRecord | None
    queued: bool = False
    reason: str = ""


@dataclass
class UsageEntry:
    ts: str
    memory_id: str
    task_id: str
    trace_id: str
    reason: str
    later_correction: bool = False


class MemoryManager(Protocol):
    def get(self, memory_id: str) -> MemoryRecord: ...
    def list(
        self,
        *,
        scope: str | None = None,
        status: str | None = None,
        type: str | None = None,
    ) -> list[MemoryRecord]: ...
    def submit(self, candidate: MemoryCandidate) -> SubmitResult: ...
    def retrieve(self, query: str, scopes: list[str], budget: int) -> list[RankedMemory]: ...
    def retract(self, memory_id: str, actor: str) -> MemoryRecord: ...
    def approve(self, memory_id: str, actor: str) -> MemoryRecord: ...
    def reject(self, memory_id: str, actor: str) -> MemoryRecord: ...
    def record_usage(self, entries: list[UsageEntry]) -> None: ...
    def usage_log(self, limit: int = 200) -> list[UsageEntry]: ...
    def ids_from_run(self, run_id: str) -> list[str]: ...
    def recently_learned(self, limit: int = 20) -> list[MemoryRecord]: ...
    def rollback_run(self, run_id: str, actor: str) -> list[str]: ...

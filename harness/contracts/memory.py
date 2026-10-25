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
    hint_notes: list[str] = field(default_factory=list)
    supersedes: str | None = None
    superseded_by: str | None = None
    run_id: str | None = None


@dataclass
class MemoryCandidate:
    """A write request from the harness.

    ``type_hint``/``scope_hint`` are untrusted suggestions (usually from the
    model). The memory core validates them, falls back to the classifier and
    default scope, and never fails the write because of a bad hint.
    ``provenance`` is harness-filled, not model-facing.
    """

    content: str
    origin: str = "explicit"
    type_hint: str | None = None
    scope_hint: str | None = None
    default_scope: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    supersedes: str | None = None
    run_id: str | None = None
    # Test/eval seeding only. Normal writes use the current time.
    updated_at: str | None = None


@dataclass
class RankedMemory:
    record: MemoryRecord
    score: float
    reason: str


@dataclass
class SubmitResult:
    accepted: bool
    record: MemoryRecord | None
    reason: str = ""
    duplicate: bool = False
    replaced: list[MemoryRecord] = field(default_factory=list)
    hint_notes: list[str] = field(default_factory=list)


@dataclass
class UsageEntry:
    ts: str
    memory_id: str
    task_id: str
    trace_id: str
    reason: str
    later_correction: bool = False


class MemoryClassifier(Protocol):
    def classify(self, content: str) -> str: ...


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
    def set_type(self, memory_id: str, type: str, actor: str) -> MemoryRecord: ...
    def restore(self, memory_id: str, actor: str) -> MemoryRecord: ...
    def consolidate(self, run_id: str, actor: str) -> list[dict[str, Any]]: ...
    def record_usage(self, entries: list[UsageEntry]) -> None: ...
    def usage_log(self, limit: int = 200) -> list[UsageEntry]: ...
    def ids_from_run(self, run_id: str) -> list[str]: ...
    def recently_learned(self, limit: int = 20) -> list[MemoryRecord]: ...
    def rollback_run(self, run_id: str, actor: str) -> list[str]: ...
    def reset(self, *, keep_records: bool = False) -> None: ...

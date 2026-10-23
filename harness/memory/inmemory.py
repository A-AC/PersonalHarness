from __future__ import annotations

from harness.contracts.events import EventLog
from harness.contracts.memory import MemoryCandidate, MemoryRecord, RankedMemory, SubmitResult, UsageEntry
from harness.contracts.settings import SettingsService
from harness.contracts.sources import SourceResolver
from harness.memory.core import MemoryCore


class InMemoryMemoryManager:
    def __init__(self, settings: SettingsService, events: EventLog, sources: SourceResolver, scope_check):
        self._records: dict[str, MemoryRecord] = {}
        self._usage: list[UsageEntry] = []
        self._core = MemoryCore(
            settings=settings,
            events=events,
            sources=sources,
            get=lambda i: self._records.get(i),
            all_records=lambda: list(self._records.values()),
            put=lambda rec: self._records.__setitem__(rec.id, rec),
            usage_put=self._usage.append,
            usage_all=lambda: list(self._usage),
            scopes=scope_check,
        )

    def get(self, memory_id: str) -> MemoryRecord:
        return self._core.get(memory_id)

    def list(self, *, scope: str | None = None, status: str | None = None, type: str | None = None) -> list[MemoryRecord]:
        return self._core.list(scope=scope, status=status, type=type)

    def submit(self, candidate: MemoryCandidate) -> SubmitResult:
        return self._core.submit(candidate)

    def retrieve(self, query: str, scopes: list[str], budget: int) -> list[RankedMemory]:
        return self._core.retrieve(query, scopes, budget)

    def retract(self, memory_id: str, actor: str) -> MemoryRecord:
        return self._core.retract(memory_id, actor)

    def approve(self, memory_id: str, actor: str) -> MemoryRecord:
        return self._core.approve(memory_id, actor)

    def reject(self, memory_id: str, actor: str) -> MemoryRecord:
        return self._core.reject(memory_id, actor)

    def record_usage(self, entries: list[UsageEntry]) -> None:
        self._core.record_usage(entries)

    def usage_log(self, limit: int = 200) -> list[UsageEntry]:
        return self._core.usage_log(limit)

    def ids_from_run(self, run_id: str) -> list[str]:
        return self._core.ids_from_run(run_id)

    def recently_learned(self, limit: int = 20) -> list[MemoryRecord]:
        return self._core.recently_learned(limit)

    def rollback_run(self, run_id: str, actor: str) -> list[str]:
        return self._core.rollback_run(run_id, actor)

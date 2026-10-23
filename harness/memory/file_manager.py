from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from harness.contracts.events import EventLog
from harness.contracts.memory import MemoryCandidate, MemoryRecord, RankedMemory, SubmitResult, UsageEntry
from harness.contracts.settings import SettingsService
from harness.contracts.sources import SourceResolver
from harness.memory.core import MemoryCore
from harness.storage.atomic import append_jsonl, atomic_write_json, read_json, read_jsonl


class FileMemoryManager:
    def __init__(self, directory: Path, settings: SettingsService, events: EventLog, sources: SourceResolver, scope_check):
        self._dir = directory
        self._records = directory / "records"
        self._usage_path = directory / "usage.jsonl"
        self._settings = settings
        self._records.mkdir(parents=True, exist_ok=True)
        self._core = MemoryCore(
            settings=settings,
            events=events,
            sources=sources,
            get=self._get,
            all_records=self._all,
            put=self._put,
            usage_put=self._usage_put,
            usage_all=self._usage_all,
            scopes=scope_check,
        )

    def _rw(self) -> tuple[int, float]:
        return int(self._settings.get("storage.atomic_retries")), float(self._settings.get("storage.atomic_retry_s"))

    def _get(self, memory_id: str) -> MemoryRecord | None:
        path = self._records / f"{memory_id}.json"
        if not path.exists():
            return None
        return MemoryRecord(**read_json(path))

    def _all(self) -> list[MemoryRecord]:
        return [MemoryRecord(**read_json(path)) for path in self._records.glob("mem_*.json")]

    def _put(self, record: MemoryRecord) -> None:
        retries, retry_s = self._rw()
        atomic_write_json(self._records / f"{record.id}.json", asdict(record), retries=retries, retry_s=retry_s)

    def _usage_put(self, entry: UsageEntry) -> None:
        retries, retry_s = self._rw()
        append_jsonl(self._usage_path, asdict(entry), retries=retries, retry_s=retry_s)

    def _usage_all(self) -> list[UsageEntry]:
        return [UsageEntry(**row) for row in read_jsonl(self._usage_path)]

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

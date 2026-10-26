from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import shutil

from harness.constants import CONVERSATION_REF_PREFIXES
from harness.contracts.events import EventLog
from harness.contracts.memory import MemoryCandidate, MemoryClassifier, MemoryRecord, RankedMemory, SemanticScorer, SubmitResult, UsageEntry
from harness.contracts.settings import SettingsService
from harness.contracts.sources import SourceResolver
from harness.memory.core import MemoryCore
from harness.storage.atomic import append_jsonl, atomic_write_json, read_json, read_jsonl


class FileMemoryManager:
    def __init__(
        self,
        directory: Path,
        settings: SettingsService,
        events: EventLog,
        sources: SourceResolver,
        scope_check,
        classifier: MemoryClassifier | None = None,
        semantic: SemanticScorer | None = None,
    ):
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
            classifier=classifier,
            semantic=semantic,
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

    def retrieve(
        self,
        query: str,
        scopes: list[str],
        budget: int,
        *,
        scope_priority: list[str] | None = None,
        kinds: list[str] | None = None,
        now: str | None = None,
    ) -> list[RankedMemory]:
        return self._core.retrieve(
            query,
            scopes,
            budget,
            scope_priority=scope_priority,
            kinds=kinds,
            now=now,
        )

    def lifecycle(self, record: MemoryRecord) -> str:
        return self._core.lifecycle(record)

    def retract(self, memory_id: str, actor: str) -> MemoryRecord:
        return self._core.retract(memory_id, actor)

    def set_type(self, memory_id: str, type: str, actor: str) -> MemoryRecord:
        return self._core.set_type(memory_id, type, actor)

    def restore(self, memory_id: str, actor: str) -> MemoryRecord:
        return self._core.restore(memory_id, actor)

    def consolidate(self, run_id: str, actor: str) -> list[dict]:
        return self._core.consolidate(run_id, actor)

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

    def reset(self, *, keep_records: bool = False) -> None:
        """Clear the store, or only conversation-derived usage/references."""
        if not keep_records:
            shutil.rmtree(self._records, ignore_errors=True)
        self._records.mkdir(parents=True, exist_ok=True)
        self._usage_path.unlink(missing_ok=True)
        if keep_records:
            self._strip_conversation_refs()

    def _strip_conversation_refs(self) -> None:
        retries, retry_s = self._rw()
        for path in self._records.glob("mem_*.json"):
            data = read_json(path)
            provenance = data.get("provenance") or {}
            refs = [str(item) for item in provenance.get("source_refs") or []]
            kept = [item for item in refs if not item.startswith(CONVERSATION_REF_PREFIXES)]
            if kept == refs:
                continue
            dropped = sorted(set(refs) - set(kept))
            provenance["source_refs"] = kept
            provenance["reset_cleared_refs"] = sorted(
                set(provenance.get("reset_cleared_refs") or []) | set(dropped)
            )
            data["provenance"] = provenance
            atomic_write_json(path, data, retries=retries, retry_s=retry_s)

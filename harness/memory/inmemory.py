from __future__ import annotations

from harness.contracts.events import EventLog
from harness.constants import CONVERSATION_REF_PREFIXES
from harness.contracts.memory import MemoryCandidate, MemoryClassifier, MemoryRecord, RankedMemory, SemanticScorer, SubmitResult, UsageEntry
from harness.contracts.settings import SettingsService
from harness.contracts.sources import SourceResolver
from harness.memory.core import MemoryCore


class InMemoryMemoryManager:
    def __init__(
        self,
        settings: SettingsService,
        events: EventLog,
        sources: SourceResolver,
        scope_check,
        classifier: MemoryClassifier | None = None,
        semantic: SemanticScorer | None = None,
    ):
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
            classifier=classifier,
            semantic=semantic,
        )

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
        self._usage.clear()
        if keep_records:
            for record in self._records.values():
                provenance = record.provenance or {}
                refs = [str(item) for item in provenance.get("source_refs") or []]
                kept = [item for item in refs if not item.startswith(CONVERSATION_REF_PREFIXES)]
                if kept == refs:
                    continue
                provenance["source_refs"] = kept
                provenance["reset_cleared_refs"] = sorted(
                    set(provenance.get("reset_cleared_refs") or [])
                    | (set(refs) - set(kept))
                )
                record.provenance = provenance
            return
        self._records.clear()

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Callable

from harness.constants import MemoryOrigin, MemoryStatus, MemoryType, SourceKind
from harness.contracts.events import Event, EventLog
from harness.contracts.memory import MemoryCandidate, MemoryRecord, RankedMemory, SubmitResult, UsageEntry
from harness.contracts.settings import SettingsService
from harness.contracts.sources import SourceResolver
from harness.memory.tokens import overlap, tokens
from harness.storage.atomic import new_id, now_ts


StoreGet = Callable[[str], MemoryRecord | None]
StoreAll = Callable[[], list[MemoryRecord]]
StorePut = Callable[[MemoryRecord], None]
UsagePut = Callable[[UsageEntry], None]
UsageAll = Callable[[], list[UsageEntry]]


class MemoryCore:
    def __init__(
        self,
        *,
        settings: SettingsService,
        events: EventLog,
        sources: SourceResolver,
        get: StoreGet,
        all_records: StoreAll,
        put: StorePut,
        usage_put: UsagePut,
        usage_all: UsageAll,
        scopes: Callable[[str], bool],
    ):
        self._settings = settings
        self._events = events
        self._sources = sources
        self._get = get
        self._all = all_records
        self._put = put
        self._usage_put = usage_put
        self._usage_all = usage_all
        self._scope_exists = scopes

    def get(self, memory_id: str) -> MemoryRecord:
        record = self._get(memory_id)
        if record is None:
            raise KeyError(memory_id)
        return record

    def list(self, *, scope: str | None = None, status: str | None = None, type: str | None = None) -> list[MemoryRecord]:
        items = self._all()
        if scope:
            items = [item for item in items if item.scope == scope]
        if status:
            items = [item for item in items if item.status == status]
        if type:
            items = [item for item in items if item.type == type]
        return sorted(items, key=lambda item: item.updated_at, reverse=True)

    def submit(self, candidate: MemoryCandidate) -> SubmitResult:
        error = self._validate_candidate(candidate)
        if error:
            self._events.append(
                Event(id=new_id("evt"), ts=now_ts(), actor="harness", operation="memory.reject_invalid", target="", result=error)
            )
            return SubmitResult(accepted=False, record=None, reason=error)

        ts = now_ts()
        ident = new_id("mem")
        record = MemoryRecord(
            id=ident,
            type=candidate.type,
            content=candidate.content,
            scope=candidate.scope,
            origin=candidate.origin,
            status=MemoryStatus.ACTIVE,
            created_at=ts,
            updated_at=ts,
            provenance=dict(candidate.provenance),
            supersedes=candidate.supersedes,
            run_id=candidate.run_id,
        )

        conflict = self._explicit_conflict(record)
        needs_queue = False
        if candidate.origin != MemoryOrigin.EXPLICIT and conflict:
            needs_queue = True
        if candidate.supersedes:
            old = self._get(candidate.supersedes)
            if old and old.origin == MemoryOrigin.EXPLICIT and candidate.origin != MemoryOrigin.EXPLICIT:
                needs_queue = True

        if needs_queue:
            record.status = MemoryStatus.PROPOSED
            self._put(record)
            self._events.append(
                Event(id=new_id("evt"), ts=ts, actor="harness", operation="memory.queue", target=record.id, result="proposed")
            )
            return SubmitResult(accepted=True, record=record, queued=True, reason="queued for approval")

        if candidate.origin != MemoryOrigin.EXPLICIT:
            if candidate.supersedes and not bool(self._settings.get("memory.auto_commit.inferred_merge")):
                record.status = MemoryStatus.PROPOSED
                self._put(record)
                return SubmitResult(accepted=True, record=record, queued=True, reason="merge queued")
            if not bool(self._settings.get("memory.auto_commit.inferred_new")):
                record.status = MemoryStatus.PROPOSED
                self._put(record)
                return SubmitResult(accepted=True, record=record, queued=True, reason="inferred queued")

        self._commit_active(record)
        return SubmitResult(accepted=True, record=record, queued=False, reason="committed")

    def _commit_active(self, record: MemoryRecord) -> None:
        duplicate = self._find_duplicate(record)
        if duplicate and record.origin != MemoryOrigin.EXPLICIT:
            record.supersedes = duplicate.id
        if record.supersedes:
            old = self._get(record.supersedes)
            if old is not None:
                old = replace(old, status=MemoryStatus.SUPERSEDED, superseded_by=record.id, updated_at=record.created_at)
                self._put(old)
        self._put(record)
        self._events.append(
            Event(id=new_id("evt"), ts=record.created_at, actor="harness", operation="memory.commit", target=record.id, result=record.status)
        )

    def retrieve(self, query: str, scopes: list[str], budget: int) -> list[RankedMemory]:
        query_tokens = tokens(query)
        explicit_bonus = float(self._settings.get("memory.retrieval.explicit_bonus"))
        preference_bonus = float(self._settings.get("memory.retrieval.preference_bonus"))
        empty_query_score = float(self._settings.get("memory.retrieval.empty_query_score"))
        ranked: list[RankedMemory] = []
        for record in self._all():
            if record.status != MemoryStatus.ACTIVE:
                continue
            if scopes and record.scope not in scopes:
                continue
            content_tokens = tokens(record.content)
            if not query_tokens:
                score = empty_query_score
                reason = "empty query fallback"
            else:
                hits = query_tokens & content_tokens
                if not hits and record.type not in {MemoryType.PREFERENCE}:
                    # still allow type/scope metadata match on shared words in scope
                    if not any(tok in record.scope for tok in query_tokens):
                        continue
                score = (len(hits) / max(1, len(query_tokens))) + (
                    explicit_bonus if record.origin == MemoryOrigin.EXPLICIT else 0.0
                )
                reason = f"keyword overlap {sorted(hits)[:8]}"
            if record.type == MemoryType.PREFERENCE:
                score += preference_bonus
            ranked.append(RankedMemory(record=record, score=score, reason=reason))
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:budget]

    def retract(self, memory_id: str, actor: str) -> MemoryRecord:
        record = self.get(memory_id)
        record = replace(record, status=MemoryStatus.RETRACTED, updated_at=now_ts())
        self._put(record)
        self._events.append(
            Event(id=new_id("evt"), ts=record.updated_at, actor=actor, operation="memory.retract", target=record.id, result="retracted")
        )
        return record

    def approve(self, memory_id: str, actor: str) -> MemoryRecord:
        record = self.get(memory_id)
        if record.status != MemoryStatus.PROPOSED:
            raise ValueError("not proposed")
        record = replace(record, status=MemoryStatus.ACTIVE, updated_at=now_ts())
        self._commit_active(record)
        self._events.append(
            Event(id=new_id("evt"), ts=record.updated_at, actor=actor, operation="memory.approve", target=record.id, result="active")
        )
        return self.get(memory_id)

    def reject(self, memory_id: str, actor: str) -> MemoryRecord:
        record = self.get(memory_id)
        record = replace(record, status=MemoryStatus.REJECTED, updated_at=now_ts())
        self._put(record)
        self._events.append(
            Event(id=new_id("evt"), ts=record.updated_at, actor=actor, operation="memory.reject", target=record.id, result="rejected")
        )
        return record

    def record_usage(self, entries: list[UsageEntry]) -> None:
        for entry in entries:
            self._usage_put(entry)

    def usage_log(self, limit: int = 200) -> list[UsageEntry]:
        return self._usage_all()[-limit:]

    def ids_from_run(self, run_id: str) -> list[str]:
        return [item.id for item in self._all() if item.run_id == run_id]

    def recently_learned(self, limit: int = 20) -> list[MemoryRecord]:
        items = [
            item
            for item in self._all()
            if item.origin in {MemoryOrigin.INFERRED, MemoryOrigin.CONSOLIDATED} and item.status == MemoryStatus.ACTIVE
        ]
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items[:limit]

    def rollback_run(self, run_id: str, actor: str) -> list[str]:
        touched: list[str] = []
        for record in list(self._all()):
            if record.run_id != run_id:
                continue
            if record.supersedes:
                old = self._get(record.supersedes)
                if old is not None and old.superseded_by == record.id:
                    restored = replace(
                        old,
                        status=MemoryStatus.ACTIVE,
                        superseded_by=None,
                        updated_at=now_ts(),
                    )
                    self._put(restored)
                    touched.append(restored.id)
            retracted = replace(record, status=MemoryStatus.RETRACTED, updated_at=now_ts())
            self._put(retracted)
            touched.append(record.id)
            self._events.append(
                Event(
                    id=new_id("evt"),
                    ts=retracted.updated_at,
                    actor=actor,
                    operation="memory.rollback_run",
                    target=record.id,
                    result="retracted",
                    refs=[run_id],
                )
            )
        return touched

    def _validate_candidate(self, candidate: MemoryCandidate) -> str | None:
        if candidate.type not in list(MemoryType):
            return "invalid type"
        if candidate.origin not in list(MemoryOrigin):
            return "invalid origin"
        if not candidate.content.strip():
            return "empty content"
        if not self._scope_exists(candidate.scope):
            return "unknown scope"
        provenance = candidate.provenance or {}
        source_kind = provenance.get("source_kind")
        if source_kind not in list(SourceKind):
            return "invalid source_kind"
        policy_key = f"memory.source_policy.{source_kind}"
        policy = str(self._settings.get(policy_key))
        if policy == "disabled":
            return f"source {source_kind} disabled"
        refs = provenance.get("source_refs") or []
        if not refs:
            return "source_refs required"
        excerpt = provenance.get("excerpt")
        for ref in refs:
            found = self._sources.excerpt(ref)
            if found is None:
                return f"unresolved source_ref {ref}"
            if excerpt and excerpt not in found:
                return "excerpt not present in source"
        if source_kind == SourceKind.EXECUTION_OUTCOME:
            allowed = {"status", "error_category", "message", "capability", "operation"}
            extra = set((provenance.get("structured_fields") or {}).keys()) - allowed
            if extra:
                return "unstructured execution text is not a source"
        return None

    def _explicit_conflict(self, record: MemoryRecord) -> bool | MemoryRecord:
        threshold = float(self._settings.get("memory.duplicate_overlap"))
        for other in self._all():
            if other.status != MemoryStatus.ACTIVE:
                continue
            if other.scope != record.scope or other.type != record.type:
                continue
            if other.origin == MemoryOrigin.EXPLICIT and overlap(other.content, record.content) >= threshold:
                if other.content.strip() != record.content.strip():
                    return True
        return False

    def _find_duplicate(self, record: MemoryRecord) -> MemoryRecord | None:
        threshold = float(self._settings.get("memory.duplicate_overlap"))
        best = None
        best_score = 0.0
        for other in self._all():
            if other.id == record.id or other.status != MemoryStatus.ACTIVE:
                continue
            if other.scope != record.scope or other.type != record.type:
                continue
            score = overlap(other.content, record.content)
            if score >= threshold and score > best_score:
                best = other
                best_score = score
        return best

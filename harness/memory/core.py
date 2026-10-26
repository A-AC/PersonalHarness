from __future__ import annotations

from dataclasses import replace
from typing import Callable

from harness.constants import MemoryKind, MemoryOrigin, MemoryStatus, MemoryType, SourceKind
from harness.contracts.events import Event, EventLog
from harness.contracts.memory import (
    MemoryCandidate,
    MemoryClassifier,
    MemoryRecord,
    RankedMemory,
    SubmitResult,
    UsageEntry,
)
from harness.contracts.settings import SettingsService
from harness.contracts.sources import SourceResolver
from harness.memory.classifier import KeywordMemoryClassifier, kind_for_type
from harness.memory.tokens import overlap, tokens
from harness.storage.atomic import new_id, now_ts
from harness.text import word_tokens


StoreGet = Callable[[str], MemoryRecord | None]
StoreAll = Callable[[], list[MemoryRecord]]
StorePut = Callable[[MemoryRecord], None]
UsagePut = Callable[[UsageEntry], None]
UsageAll = Callable[[], list[UsageEntry]]


def normalize_content(text: str) -> str:
    return " ".join(word_tokens(text))


def containment(a: str, b: str) -> float:
    """Share of the smaller token set that appears in the other.

    Jaccard overlap misses a one-word substitution in an otherwise identical
    statement ("name is Alex" vs "name is Bob"); containment catches it.
    """
    left, right = word_tokens(a), word_tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


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
        classifier: MemoryClassifier | None = None,
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
        self._classifier = classifier or KeywordMemoryClassifier(settings)

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
        content = (candidate.content or "").strip()
        error = self._validate_candidate(candidate, content)
        if error:
            self._events.append(
                Event(id=new_id("evt"), ts=now_ts(), actor="harness", operation="memory.reject_invalid", target="", result=error)
            )
            return SubmitResult(accepted=False, record=None, reason=error)

        notes: list[str] = []
        assigned_type = self._assign_type(candidate.type_hint, content, notes)
        assigned_kind = self._assign_kind(candidate.kind_hint, assigned_type, notes)
        if assigned_kind == MemoryKind.PROCEDURAL and assigned_type not in {
            MemoryType.PROCEDURE.value,
            MemoryType.LESSON.value,
        }:
            # A procedural hint can arrive with a semantic-looking classifier
            # result (for example an execution-outcome sentence). Keep the
            # kind/type axes consistent without touching semantic writes or
            # explicit remember calls, which never pass a kind hint.
            notes.append(f'procedural memory type "{assigned_type}" normalized to "lesson"')
            assigned_type = MemoryType.LESSON.value
        scope = self._resolve_scope(candidate, notes)

        existing = self._find_exact(content, scope, assigned_kind)
        if existing is not None:
            replaced: list[MemoryRecord] = []
            supersedes = candidate.supersedes
            if supersedes and supersedes != existing.id:
                # A repeated resolution sentence is still allowed to retire the
                # new failure it resolves, even though the content duplicates an
                # earlier resolution lesson.
                target = self._get(supersedes)
                if (
                    target is not None
                    and target.status == MemoryStatus.ACTIVE
                    and target.kind == assigned_kind
                ):
                    ts = now_ts()
                    old = replace(
                        target,
                        status=MemoryStatus.SUPERSEDED,
                        superseded_by=existing.id,
                        updated_at=ts,
                    )
                    self._put(old)
                    replaced.append(old)
                    self._events.append(
                        Event(
                            id=new_id("evt"),
                            ts=ts,
                            actor="harness",
                            operation="memory.supersede",
                            target=old.id,
                            result=existing.id,
                        )
                    )
            self._events.append(
                Event(id=new_id("evt"), ts=now_ts(), actor="harness", operation="memory.duplicate", target=existing.id, result="active")
            )
            return SubmitResult(
                accepted=True,
                record=existing,
                reason="already remembered",
                duplicate=True,
                replaced=replaced,
                hint_notes=notes,
            )

        conflict = (
            self._find_conflict(content, scope, assigned_type, assigned_kind)
            if candidate.origin == MemoryOrigin.EXPLICIT
            else None
        )
        supersedes = candidate.supersedes
        if supersedes:
            target = self._get(supersedes)
            if target is not None and target.kind != assigned_kind:
                notes.append(
                    f'supersedes target is kind "{target.kind}", not "{assigned_kind}"; not superseded'
                )
                supersedes = None
        ts = candidate.updated_at or now_ts()
        record = MemoryRecord(
            id=new_id("mem"),
            type=assigned_type,
            kind=assigned_kind,
            content=content,
            scope=scope,
            origin=candidate.origin,
            status=MemoryStatus.ACTIVE,
            created_at=ts,
            updated_at=ts,
            provenance=dict(candidate.provenance),
            hint_notes=notes,
            supersedes=supersedes or (conflict.id if conflict else None),
            run_id=candidate.run_id,
        )
        replaced: list[MemoryRecord] = []
        if conflict is not None:
            old = replace(
                conflict,
                status=MemoryStatus.SUPERSEDED,
                superseded_by=record.id,
                updated_at=ts,
            )
            self._put(old)
            replaced.append(old)
            self._events.append(
                Event(id=new_id("evt"), ts=ts, actor="harness", operation="memory.supersede", target=old.id, result=record.id)
            )
        elif supersedes:
            old = self._get(supersedes)
            if old is not None:
                old = replace(old, status=MemoryStatus.SUPERSEDED, superseded_by=record.id, updated_at=ts)
                self._put(old)
                replaced.append(old)
                self._events.append(
                    Event(id=new_id("evt"), ts=ts, actor="harness", operation="memory.supersede", target=old.id, result=record.id)
                )

        self._put(record)
        self._events.append(
            Event(id=new_id("evt"), ts=ts, actor="harness", operation="memory.commit", target=record.id, result=record.status)
        )
        reason = "committed"
        if replaced:
            reason = "replaced previous memory"
        return SubmitResult(
            accepted=True,
            record=record,
            reason=reason,
            replaced=replaced,
            hint_notes=notes,
        )

    def _assign_type(self, hint: str | None, content: str, notes: list[str]) -> str:
        allowed = {item.value for item in MemoryType}
        classified = str(self._classifier.classify(content) or "").strip()
        if classified not in allowed:
            classified = str(self._settings.get("memory.classifier.default_type"))
        if classified not in allowed:
            classified = MemoryType.FACT.value
        value = str(hint or "").strip()
        if value and value not in allowed:
            notes.append(f'type hint "{value}" is not a valid type; used "{classified}"')
        elif value and value != classified:
            notes.append(f'type hint "{value}" was ignored; classified as "{classified}"')
        return classified

    def _assign_kind(self, hint: str | None, assigned_type: str, notes: list[str]) -> str:
        allowed = {item.value for item in MemoryKind}
        derived = kind_for_type(assigned_type)
        value = str(hint or "").strip()
        if not value:
            return derived
        if value not in allowed:
            notes.append(f'kind hint "{value}" is not a valid kind; used "{derived}"')
            return derived
        if value != derived:
            notes.append(f'kind hint "{value}" overrides derived kind "{derived}"')
        return value

    def _resolve_scope(self, candidate: MemoryCandidate, notes: list[str]) -> str:
        fallback = str(candidate.default_scope or self._settings.get("memory.default_scope") or "global")
        if not self._scope_exists(fallback):
            fallback = "global"
        hint = str(candidate.scope_hint or "").strip()
        if not hint:
            return fallback
        if not self._scope_exists(hint):
            notes.append(f'scope hint "{hint}" is not a known scope; used "{fallback}"')
            return fallback
        if hint == "self" and fallback != "self":
            notes.append(
                'scope hint "self" is only for memories about the assistant; '
                f'used "{fallback}"'
            )
            return fallback
        return hint

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
                if not hits and record.type != MemoryType.PREFERENCE:
                    continue
                score = (len(hits) / max(1, len(query_tokens))) + (
                    explicit_bonus if record.origin == MemoryOrigin.EXPLICIT else 0.0
                )
                reason = f"keyword overlap {sorted(hits)[:8]}"
            if record.type == MemoryType.PREFERENCE:
                score += preference_bonus
            ranked.append(RankedMemory(record=record, score=score, reason=reason))
        if bool(self._settings.get("memory.retrieval.recency_tiebreak")):
            ranked.sort(key=lambda item: (item.score, item.record.updated_at, item.record.id), reverse=True)
        else:
            ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:budget]

    def retract(self, memory_id: str, actor: str) -> MemoryRecord:
        record = self.get(memory_id)
        if record.status == MemoryStatus.RETRACTED:
            return record
        record = replace(record, status=MemoryStatus.RETRACTED, updated_at=now_ts())
        self._put(record)
        self._events.append(
            Event(id=new_id("evt"), ts=record.updated_at, actor=actor, operation="memory.retract", target=record.id, result="retracted")
        )
        return record

    def set_type(self, memory_id: str, type: str, actor: str) -> MemoryRecord:
        if type not in {item.value for item in MemoryType}:
            raise ValueError(f"invalid memory type: {type}")
        record = self.get(memory_id)
        if record.type == type:
            return record
        record = replace(record, type=type, updated_at=now_ts())
        self._put(record)
        self._events.append(
            Event(id=new_id("evt"), ts=record.updated_at, actor=actor, operation="memory.retype", target=record.id, result=type)
        )
        return record

    def restore(self, memory_id: str, actor: str) -> MemoryRecord:
        record = self.get(memory_id)
        record = replace(record, status=MemoryStatus.ACTIVE, superseded_by=None, updated_at=now_ts())
        self._put(record)
        # A restored record is no longer superseded, so a surviving winner must
        # not keep pointing at it. This keeps history honest after undo.
        for other in self._all():
            if other.id != record.id and other.supersedes == record.id:
                self._put(replace(other, supersedes=None, updated_at=now_ts()))
        self._events.append(
            Event(id=new_id("evt"), ts=record.updated_at, actor=actor, operation="memory.restore", target=record.id, result="active")
        )
        return record

    def consolidate(self, run_id: str, actor: str) -> list[dict]:
        """Deterministic maintenance: re-type, merge, and resolve by recency.

        No model is involved. The newest active record wins; older records keep
        their content and history as ``superseded``.
        """
        results: list[dict] = []
        if bool(self._settings.get("memory.consolidation.retype")):
            for record in list(self._all()):
                if record.status != MemoryStatus.ACTIVE:
                    continue
                # Only the semantic subtype is classifier-owned. Procedural,
                # episodic, and prospective rows keep their type and kind.
                if record.kind != MemoryKind.SEMANTIC:
                    continue
                new_type = self._assign_type(None, record.content, [])
                if kind_for_type(new_type) != record.kind:
                    continue
                if new_type != record.type:
                    old_type = record.type
                    updated = replace(record, type=new_type, updated_at=now_ts())
                    self._put(updated)
                    results.append(
                        {
                            "kind": "retype",
                            "memory_id": record.id,
                            "from": old_type,
                            "to": new_type,
                            "status": "retyped",
                        }
                    )

        threshold = float(self._settings.get("memory.duplicate_overlap"))
        records = sorted(
            [item for item in self._all() if item.status == MemoryStatus.ACTIVE],
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )
        consumed: set[str] = set()
        for index, newer in enumerate(records):
            if newer.id in consumed:
                continue
            current = newer
            for older in records[index + 1 :]:
                if older.id in consumed or older.status != MemoryStatus.ACTIVE:
                    continue
                if older.scope != current.scope:
                    continue
                if older.kind != current.kind:
                    continue
                if normalize_content(older.content) == normalize_content(current.content):
                    kind = "merged"
                elif overlap(older.content, current.content) >= threshold:
                    kind = "merged"
                elif older.type == current.type and self._is_conflict(older.content, current.content):
                    kind = "superseded"
                else:
                    continue
                ts = now_ts()
                loser = replace(
                    older,
                    status=MemoryStatus.SUPERSEDED,
                    superseded_by=current.id,
                    updated_at=ts,
                )
                self._put(loser)
                current = replace(current, supersedes=older.id, updated_at=ts)
                self._put(current)
                consumed.add(older.id)
                results.append(
                    {
                        "kind": kind,
                        "winner_id": current.id,
                        "loser_id": older.id,
                        "status": kind,
                    }
                )
                self._events.append(
                    Event(
                        id=new_id("evt"),
                        ts=ts,
                        actor=actor,
                        operation="memory.consolidate",
                        target=older.id,
                        result=kind,
                        refs=[run_id, current.id],
                    )
                )
        return results

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
        records = list(self._all())
        for record in records:
            if record.run_id != run_id:
                continue
            for old in records:
                if old.superseded_by == record.id and old.status == MemoryStatus.SUPERSEDED:
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

    def _validate_candidate(self, candidate: MemoryCandidate, content: str) -> str | None:
        if candidate.origin not in {item.value for item in MemoryOrigin}:
            return "invalid origin"
        if not content:
            return "empty content"
        provenance = candidate.provenance or {}
        source_kind = provenance.get("source_kind")
        if source_kind not in {item.value for item in SourceKind}:
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
            found = self._sources.excerpt(str(ref))
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

    def _find_exact(self, content: str, scope: str, kind: str) -> MemoryRecord | None:
        normalized = normalize_content(content)
        for record in self._all():
            if record.status != MemoryStatus.ACTIVE or record.scope != scope:
                continue
            if record.kind != kind:
                continue
            if normalize_content(record.content) == normalized:
                return record
        return None

    def _find_conflict(self, content: str, scope: str, type: str, kind: str) -> MemoryRecord | None:
        best: MemoryRecord | None = None
        best_score = 0.0
        for record in self._all():
            if record.status != MemoryStatus.ACTIVE or record.scope != scope:
                continue
            if record.type != type or record.kind != kind:
                continue
            if normalize_content(record.content) == normalize_content(content):
                continue
            score = containment(record.content, content)
            if score >= best_score and self._is_conflict(record.content, content):
                best = record
                best_score = score
        return best

    def _is_conflict(self, left: str, right: str) -> bool:
        """Two statements about the same subject with a changed predicate.

        Containment alone collides distinct short facts that share filler words
        ("the NAS is called vault" vs "the NAS is at 192.168.1.10"), so require
        whole-statement overlap as well. A one-word change keeps both scores
        high; a different predicate does not.
        """
        min_containment = float(self._settings.get("memory.conflict_containment"))
        min_overlap = float(self._settings.get("memory.conflict_overlap"))
        return containment(left, right) >= min_containment and overlap(left, right) >= min_overlap

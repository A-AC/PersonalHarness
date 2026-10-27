from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import Callable

from harness.constants import (
    EvidenceStatus,
    MemoryKind,
    MemoryLifecycle,
    MemoryOrigin,
    MemoryStatus,
    MemoryType,
    SourceKind,
)
from harness.contracts.events import Event, EventLog
from harness.contracts.memory import (
    MemoryCandidate,
    MemoryClassifier,
    MemoryRecord,
    RankedMemory,
    SemanticScorer,
    SubmitResult,
    UsageEntry,
)
from harness.contracts.settings import SettingsService
from harness.contracts.sources import SourceResolver
from harness.memory.classifier import KeywordMemoryClassifier, kind_for_type
from harness.memory.semantic import STOPWORDS
from harness.memory.tokens import overlap, tokens
from harness.storage.atomic import new_id, now_ts
from harness.text import word_tokens
from harness.timefmt import gap_between, parse_ts

IDENTIFIER_RE = re.compile(
    r"(?:\b\d{1,3}(?:\.\d{1,3}){3}\b"
    r"|\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+\b"
    r"|\b(?:[a-z]:\\|/)[^\s]+"
    r"|\b[a-z]+_[a-z0-9_]+\b"
    r"|\b[a-z]+-\d{2,}\b)",
    re.IGNORECASE,
)



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
        semantic: SemanticScorer | None = None,
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
        self._semantic = semantic

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
            evidence_status=str(
                candidate.evidence_status
                or candidate.provenance.get("evidence_status")
                or EvidenceStatus.STATED.value
            ),
            confidence=(
                1.0
                if candidate.confidence is None
                else max(0.0, min(1.0, float(candidate.confidence)))
            ),
            valid_from=candidate.valid_from,
            valid_until=candidate.valid_until,
            tags=list(candidate.tags),
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
        """Rank active memories for one query.

        The score is additive and explainable. Exact identifiers are a strong
        signal; lexical overlap and optional semantic similarity supply the
        conceptual match; scope, recency, provenance, confidence, and kind
        usefulness break ties. Superseded/retracted records and expired
        temporary facts never enter the candidate set, so an older statement
        cannot win merely by sharing more words.
        """
        query_text = query or ""
        query_tokens = {item for item in tokens(query_text) if item not in STOPWORDS}
        query_identifiers = {item.lower() for item in IDENTIFIER_RE.findall(query_text)}
        priority = set(scope_priority or [])
        allowed_kinds = set(kinds or [])
        reference = now or now_ts()
        reference_dt = parse_ts(reference)

        weights = {
            "lexical": float(self._settings.get("memory.retrieval.lexical_weight")),
            "semantic": float(self._settings.get("memory.retrieval.semantic_weight")),
            "exact": float(self._settings.get("memory.retrieval.exact_identifier_bonus")),
            "scope": float(self._settings.get("memory.retrieval.scope_priority_bonus")),
            "other_scope": float(self._settings.get("memory.retrieval.other_scope_penalty")),
            "recency": float(self._settings.get("memory.retrieval.recency_weight")),
            "provenance": float(self._settings.get("memory.retrieval.provenance_weight")),
            "kind": float(self._settings.get("memory.retrieval.kind_weight")),
        }
        explicit_bonus = float(self._settings.get("memory.retrieval.explicit_bonus"))
        preference_bonus = float(self._settings.get("memory.retrieval.preference_bonus"))
        empty_query_score = float(self._settings.get("memory.retrieval.empty_query_score"))
        half_life = max(0.1, float(self._settings.get("memory.retrieval.recency_half_life_days")))
        min_score = float(self._settings.get("memory.retrieval.min_score"))
        temporal_query = any(
            item in query_text.lower()
            for item in ("recent", "latest", "yesterday", "today", "last", "this week")
        )
        commitment_query = any(
            item in query_text.lower()
            for item in ("will", "tomorrow", "plan", "remind", "commit", "promise")
        )
        procedural_query = any(
            item in query_text.lower()
            for item in ("how", "fix", "restart", "procedure", "error", "fail", "lesson", "step")
        )

        ranked: list[RankedMemory] = []
        for record in self._all():
            if record.status != MemoryStatus.ACTIVE:
                continue
            if scopes and record.scope not in scopes:
                continue
            if allowed_kinds and record.kind not in allowed_kinds:
                continue
            if not self._temporally_valid(record, reference):
                continue
            if record.superseded_by and self._active_exists(record.superseded_by):
                # Defensive: an inconsistent active row whose replacement is
                # live must not override that replacement.
                continue

            content_tokens = {item for item in tokens(record.content) if item not in STOPWORDS}
            hits = query_tokens & content_tokens
            content_identifiers = {item.lower() for item in IDENTIFIER_RE.findall(record.content)}
            exact_hits = query_identifiers & content_identifiers
            normalized_query = " ".join(word_tokens(query_text))
            normalized_content = " ".join(word_tokens(record.content))
            phrase_exact = bool(
                normalized_query
                and len(normalized_query.split()) >= 3
                and normalized_query in normalized_content
            )

            if not query_tokens:
                lexical = empty_query_score
            else:
                lexical = len(hits) / max(1, len(query_tokens))
            semantic = (
                self._semantic.score(query_text, record.content)
                if self._semantic is not None
                else 0.0
            )
            if not hits and not exact_hits and not phrase_exact and record.type != MemoryType.PREFERENCE:
                # Optional semantic similarity is the only reason an unrelated
                # lexical candidate may still be useful.
                if semantic <= 0.0:
                    continue
            exact = weights["exact"] if (exact_hits or phrase_exact) else 0.0
            scope_score = 0.0
            if priority:
                if record.scope in priority:
                    scope_score = weights["scope"]
                elif record.scope.startswith("domain:"):
                    scope_score = -weights["other_scope"]
            recency = self._recency_score(record.updated_at, reference_dt, half_life) * weights["recency"]
            provenance = self._provenance_score(record) * weights["provenance"]
            kind_score = self._kind_score(
                record,
                procedural_query=procedural_query,
                temporal_query=temporal_query,
                commitment_query=commitment_query,
            ) * weights["kind"]
            score = (
                lexical * weights["lexical"]
                + semantic * weights["semantic"]
                + exact
                + scope_score
                + recency
                + provenance
                + kind_score
                + (explicit_bonus if record.origin == MemoryOrigin.EXPLICIT else 0.0)
                + (preference_bonus if record.type == MemoryType.PREFERENCE else 0.0)
            )
            if query_tokens and lexical <= 0 and not exact_hits and semantic <= 0 and record.type != MemoryType.PREFERENCE:
                continue
            if score < min_score and record.type != MemoryType.PREFERENCE:
                continue

            reason_parts: list[str] = []
            if exact_hits:
                reason_parts.append("exact identifier " + ", ".join(sorted(exact_hits)))
            elif phrase_exact:
                reason_parts.append("exact phrase")
            if hits:
                reason_parts.append("keyword " + ",".join(sorted(hits)[:6]))
            if semantic > 0:
                reason_parts.append(f"semantic {semantic:.2f}")
            if record.scope in priority:
                reason_parts.append("scope relevant")
            reason_parts.append(f"score {score:.2f}")
            ranked.append(
                RankedMemory(
                    record=record,
                    score=score,
                    reason="; ".join(reason_parts),
                    score_parts={
                        "lexical": lexical,
                        "semantic": semantic,
                        "exact": exact,
                        "scope": scope_score,
                        "recency": recency,
                        "provenance": provenance,
                        "kind": kind_score,
                    },
                )
            )

        ranked = self._suppress_conflicts(ranked)
        if bool(self._settings.get("memory.retrieval.recency_tiebreak")):
            ranked.sort(
                key=lambda item: (item.score, item.record.updated_at, item.record.id),
                reverse=True,
            )
        else:
            ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[: max(0, int(budget))]

    def _active_exists(self, memory_id: str) -> bool:
        record = self._get(memory_id)
        return record is not None and record.status == MemoryStatus.ACTIVE

    @staticmethod
    def _temporally_valid(record: MemoryRecord, now: str) -> bool:
        now_dt = parse_ts(now)
        if now_dt is None:
            return True
        start = parse_ts(record.valid_from)
        end = parse_ts(record.valid_until)
        if start is not None and start > now_dt:
            return False
        if end is not None and end < now_dt:
            return False
        return True

    @staticmethod
    def _recency_score(updated_at: str, now, half_life_days: float) -> float:
        if now is None:
            return 0.0
        age = gap_between(updated_at, now.isoformat().replace("+00:00", "Z"))
        if age <= 0:
            return 1.0
        return math.pow(0.5, age / (half_life_days * 86400))

    @staticmethod
    def _provenance_score(record: MemoryRecord) -> float:
        evidence = str(
            record.evidence_status
            or (record.provenance or {}).get("evidence_status")
            or EvidenceStatus.STATED.value
        )
        evidence_weight = {
            EvidenceStatus.STATED.value: 1.0,
            EvidenceStatus.CORROBORATED.value: 1.0,
            EvidenceStatus.OBSERVED.value: 0.85,
            EvidenceStatus.INFERRED.value: 0.35,
        }.get(evidence, 0.5)
        origin_weight = {
            MemoryOrigin.EXPLICIT.value: 1.0,
            MemoryOrigin.CONSOLIDATED.value: 0.65,
            MemoryOrigin.INFERRED.value: 0.45,
        }.get(record.origin, 0.5)
        return evidence_weight * origin_weight * max(0.0, min(1.0, record.confidence))

    @staticmethod
    def _kind_score(
        record: MemoryRecord,
        *,
        procedural_query: bool,
        temporal_query: bool,
        commitment_query: bool,
    ) -> float:
        if record.kind == MemoryKind.PROCEDURAL and procedural_query:
            return 1.0
        if record.kind == MemoryKind.EPISODIC and (temporal_query or record.valid_until):
            return 1.0
        if record.kind == MemoryKind.PROSPECTIVE and commitment_query:
            return 1.0
        if record.type == MemoryType.DECISION and commitment_query:
            return 0.5
        return 0.0

    def _suppress_conflicts(self, ranked: list[RankedMemory]) -> list[RankedMemory]:
        """Drop an active row that a newer active row on the same subject wins.

        Normal writes mark the loser ``superseded``, but restored or imported
        data can leave two active rows. The newer statement wins here as well;
        lexical/semantic score is not allowed to resurrect the older fact.
        """
        kept: list[RankedMemory] = []
        for item in ranked:
            loser = False
            for other in ranked:
                if other.record.id == item.record.id:
                    continue
                if other.record.supersedes == item.record.id:
                    loser = True
                    break
                if (
                    other.record.scope == item.record.scope
                    and other.record.kind == item.record.kind
                    and other.record.type == item.record.type
                    and other.record.updated_at >= item.record.updated_at
                    and other.record.content != item.record.content
                    and self._is_conflict(other.record.content, item.record.content)
                ):
                    loser = True
                    break
            if not loser:
                kept.append(item)
        return kept

    def lifecycle(self, record: MemoryRecord) -> str:
        """Return the derived lifecycle category for a record."""
        if record.status == MemoryStatus.SUPERSEDED:
            return MemoryLifecycle.SUPERSEDED.value
        if record.status == MemoryStatus.RETRACTED:
            return MemoryLifecycle.RETRACTED.value
        if record.kind == MemoryKind.PROSPECTIVE:
            return MemoryLifecycle.COMMITMENT.value
        if record.kind == MemoryKind.EPISODIC:
            return MemoryLifecycle.TEMPORARY_EVENT.value
        if record.type == MemoryType.PREFERENCE:
            return MemoryLifecycle.PREFERENCE.value
        if record.type == MemoryType.DECISION:
            return MemoryLifecycle.DECISION.value
        if record.type in {MemoryType.PROCEDURE, MemoryType.LESSON}:
            return MemoryLifecycle.LESSON.value
        evidence = str(
            record.evidence_status
            or (record.provenance or {}).get("evidence_status")
            or EvidenceStatus.STATED.value
        )
        if (
            record.origin != MemoryOrigin.EXPLICIT
            and evidence in {EvidenceStatus.INFERRED.value, EvidenceStatus.OBSERVED.value}
        ) or record.confidence < 0.6:
            return MemoryLifecycle.HYPOTHESIS.value
        return MemoryLifecycle.VERIFIED_FACT.value

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
                if older.type != current.type:
                    # Wording overlap alone must not merge records of
                    # different subtypes (a fact is not a preference, a
                    # procedure is not a lesson); they stay separate.
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

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from harness.contracts.actions import ExecutionResult


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
    kind: str = ""
    hint_notes: list[str] = field(default_factory=list)
    supersedes: str | None = None
    superseded_by: str | None = None
    run_id: str | None = None
    # Lifecycle metadata is orthogonal to type/kind. Legacy rows default to a
    # stated, fully confident record with no explicit validity window.
    evidence_status: str = "stated"
    confidence: float = 1.0
    valid_from: str | None = None
    valid_until: str | None = None
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Legacy rows have no ``kind``. Derive it from the type so file and
        # in-memory stores behave identically without a migration write. This
        # mirrors constants.memory_kind_for_type; contracts may not import it.
        if not self.kind:
            self.kind = "procedural" if self.type in {"procedure", "lesson"} else "semantic"
        self.evidence_status = str(self.evidence_status or "stated")
        try:
            self.confidence = max(0.0, min(1.0, float(self.confidence)))
        except (TypeError, ValueError):
            self.confidence = 1.0


@dataclass
class MemoryCandidate:
    """A write request from the harness.

    ``type_hint``/``kind_hint``/``scope_hint`` are untrusted suggestions
    (usually from the model or a capture hook). The memory core validates them,
    falls back to the classifier and default scope, and never fails the write
    because of a bad hint. ``provenance`` is harness-filled, not model-facing.
    """

    content: str
    origin: str = "explicit"
    type_hint: str | None = None
    kind_hint: str | None = None
    scope_hint: str | None = None
    default_scope: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    supersedes: str | None = None
    run_id: str | None = None
    evidence_status: str | None = None
    confidence: float | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    tags: list[str] = field(default_factory=list)
    # Test/eval seeding only. Normal writes use the current time.
    updated_at: str | None = None


@dataclass
class RankedMemory:
    record: MemoryRecord
    score: float
    reason: str
    # Per-component scores kept for trace/usage explanation. Every component is
    # additive except the final score clamp.
    score_parts: dict[str, float] = field(default_factory=dict)


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


@dataclass
class OutcomeCapture:
    """Result of one execution-outcome capture attempt.

    ``created`` is false when an existing active lesson already covers the same
    fingerprint; ``memory_id`` still points at it so a later resolving success
    can supersede the right record.
    """

    memory_id: str
    created: bool
    fingerprint: dict[str, Any]


class MemoryClassifier(Protocol):
    def classify(self, content: str) -> str: ...


class SemanticScorer(Protocol):
    """Optional similarity component for retrieval.

    Implementations must be deterministic and must not require a network or
    model dependency. The core works when no scorer is supplied; the default
    implementation is a lightweight lexical-semantic scorer, so swapping in an
    embedding-backed scorer is an optional integration rather than a baseline
    requirement.
    """

    name: str

    def score(self, query: str, content: str) -> float: ...


class ExecutionOutcomeRecorder(Protocol):
    """Turns real execution outcomes into low-noise procedural memories."""

    def record(
        self,
        execution: ExecutionResult,
        *,
        domain_id: str | None,
        self_request: bool,
        prior_failure: ExecutionResult | None = None,
        prior_failure_memory_id: str | None = None,
    ) -> OutcomeCapture | None: ...


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
    def retrieve(
        self,
        query: str,
        scopes: list[str],
        budget: int,
        *,
        scope_priority: list[str] | None = None,
        kinds: list[str] | None = None,
        now: str | None = None,
    ) -> list[RankedMemory]: ...
    def retract(self, memory_id: str, actor: str) -> MemoryRecord: ...
    def set_type(self, memory_id: str, type: str, actor: str) -> MemoryRecord: ...
    def restore(self, memory_id: str, actor: str) -> MemoryRecord: ...
    def lifecycle(self, record: MemoryRecord) -> str: ...
    def consolidate(self, run_id: str, actor: str) -> list[dict[str, Any]]: ...
    def record_usage(self, entries: list[UsageEntry]) -> None: ...
    def usage_log(self, limit: int = 200) -> list[UsageEntry]: ...
    def ids_from_run(self, run_id: str) -> list[str]: ...
    def recently_learned(self, limit: int = 20) -> list[MemoryRecord]: ...
    def rollback_run(self, run_id: str, actor: str) -> list[str]: ...
    def reset(self, *, keep_records: bool = False) -> None: ...

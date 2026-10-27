from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class LearningSignal:
    """One observation offered to the learning pipeline.

    ``source_refs`` must point at persisted rows (interaction ids, attempt
    operation ids, task ids) so every retained change can be traced back to
    the original evidence. ``evidence`` carries structured fields such as a
    workflow step list, an execution fingerprint, or a persona draft; the
    pipeline validates the shape it needs per signal kind.
    """

    kind: str
    content: str
    origin: str = "harness"  # user | harness | model | scheduler
    source_refs: list[str] = field(default_factory=list)
    scope_hint: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    actor: str = "harness"
    observed_at: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class LearningOutcome:
    """The pipeline's verdict for one signal.

    ``decision`` is the authorization level the change landed at; ``refs``
    point at the created artifacts (memory id, skill id, proposal id) so the
    change can be audited and rolled back.
    """

    decision: str
    category: str
    rationale: str
    memory_id: str | None = None
    skill_id: str | None = None
    proposal_id: str | None = None
    journal_id: str | None = None
    hint_notes: list[str] = field(default_factory=list)


@dataclass
class JournalEntry:
    """The traceable record of one learning decision.

    Every learned change (applied, staged, or rejected) writes one entry
    linking the signal, its source evidence, and the resulting artifact.
    """

    id: str
    ts: str
    actor: str
    signal_kind: str
    category: str
    decision: str
    content: str
    rationale: str = ""
    source_refs: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillVersion:
    """One stored version of a skill's text, kept for review and rollback."""

    version: int
    body: str
    preconditions: list[str] = field(default_factory=list)
    description: str = ""
    ts: str = ""
    actor: str = ""
    note: str = ""


@dataclass
class SkillRecord:
    """A learned skill and its lifecycle.

    Candidates live outside the executor's discovery directory and are never
    disclosed to the model. ``provenance`` keeps why the skill exists and the
    evidence rows (task/attempt/interaction ids) that produced it.
    ``test_evidence`` is harness-verified, never a bare success claim.
    """

    id: str
    name: str
    description: str
    preconditions: list[str] = field(default_factory=list)
    status: str = "candidate"
    created_at: str = ""
    updated_at: str = ""
    actor: str = "harness"
    provenance: dict[str, Any] = field(default_factory=dict)
    test_evidence: dict[str, Any] | None = None
    supersedes: str | None = None
    versions: list[SkillVersion] = field(default_factory=list)


@dataclass
class SkillDraft:
    """A proposed skill text, generated from verified workflow evidence."""

    name: str
    description: str
    body: str
    preconditions: list[str] = field(default_factory=list)
    reason: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)
    supersedes: str | None = None


@dataclass
class ReviewItem:
    kind: str
    action: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewReport:
    ts: str
    trigger: str
    items: list[ReviewItem] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    changed: bool = False
    notes: list[str] = field(default_factory=list)


class LearningJournal(Protocol):
    def append(self, entry: JournalEntry) -> JournalEntry: ...
    def get(self, entry_id: str) -> JournalEntry: ...
    def list(
        self,
        *,
        limit: int = 100,
        decision: str | None = None,
        kind: str | None = None,
    ) -> list[JournalEntry]: ...
    def reset(self) -> None: ...


class SkillLibrary(Protocol):
    def propose(self, draft: SkillDraft, *, actor: str) -> SkillRecord: ...
    def record_test(self, skill_id: str, evidence: dict[str, Any], *, actor: str) -> SkillRecord: ...
    def approve(self, skill_id: str, *, actor: str) -> SkillRecord: ...
    def reject(self, skill_id: str, *, actor: str) -> SkillRecord: ...
    def retire(self, skill_id: str, *, actor: str) -> SkillRecord: ...
    def rollback(self, skill_id: str, *, actor: str) -> SkillRecord: ...
    def get(self, skill_id: str) -> SkillRecord: ...
    def get_by_name(self, name: str) -> SkillRecord | None: ...
    def list(self, *, status: str | None = None) -> list[SkillRecord]: ...


class LearningPipeline(Protocol):
    def process(self, signal: LearningSignal) -> LearningOutcome: ...


class LearningReview(Protocol):
    def review(self, *, trigger: str) -> ReviewReport: ...

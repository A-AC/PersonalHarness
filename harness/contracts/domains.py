from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Domain:
    id: str
    name: str
    status: str
    created_at: str
    created_by: str
    created_reason: str
    evidence: str
    capability_names: list[str]
    archived: bool = False


@dataclass
class StructureDraft:
    name: str
    reason: str
    evidence: str
    capability_names: list[str] = field(default_factory=list)


@dataclass
class Suggestion:
    id: str
    draft: StructureDraft
    ts: str
    status: str
    cooldown_until: str | None = None


@dataclass
class DomainMatch:
    """A temporary relevance judgement, never a persisted association."""

    domain_id: str | None
    name: str
    score: float
    reason: str
    confident: bool = False
    ambiguous: bool = False
    candidates: list[str] = field(default_factory=list)
    evidence: dict[str, float] = field(default_factory=dict)


class DomainInference(Protocol):
    """Infers likely existing projects from a request.

    The active domain is only one input and never an absolute routing
    constraint. Implementations must not create, rename, or move domains,
    tasks, or memories.
    """

    def infer(
        self,
        query: str,
        *,
        active_domain_id: str | None = None,
    ) -> DomainMatch: ...

    def infer_many(
        self,
        query: str,
        *,
        active_domain_id: str | None = None,
        limit: int | None = None,
    ) -> list[DomainMatch]: ...

    def summary(self, domain_id: str, *, limit: int = 4) -> str: ...


class DomainRegistry(Protocol):
    def ensure_self(self) -> Domain: ...
    def get_domain(self, domain_id: str) -> Domain: ...
    def find_domain_by_name(self, name: str) -> Domain | None: ...
    def list_domains(self, *, include_archived: bool = False) -> list[Domain]: ...
    def create_domain(self, draft: StructureDraft, *, confirmed: bool) -> Domain: ...
    def rename(self, item_id: str, name: str) -> None: ...
    def archive(self, item_id: str) -> None: ...
    def record_suggestion(self, draft: StructureDraft) -> Suggestion: ...
    def decline_suggestion(self, suggestion_id: str) -> Suggestion: ...
    def confirm_suggestion(self, suggestion_id: str) -> Domain: ...
    def list_suggestions(self) -> list[Suggestion]: ...
    def enablement(self, domain_id: str) -> list[str]: ...
    def set_enablement(self, domain_id: str, capability_names: list[str]) -> None: ...
    def suggestion_on_cooldown(self, key: str) -> bool: ...

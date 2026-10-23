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
    memory_scope_id: str
    archived: bool = False


@dataclass
class Project:
    id: str
    name: str
    domain_id: str
    status: str
    created_at: str
    created_by: str
    created_reason: str
    evidence: str
    workspace_id: str | None = None
    archived: bool = False


@dataclass
class Workspace:
    id: str
    name: str
    project_id: str
    created_at: str
    notes: str = ""
    archived: bool = False


@dataclass
class StructureDraft:
    kind: str
    name: str
    reason: str
    evidence: str
    domain_id: str | None = None
    capability_names: list[str] = field(default_factory=list)


@dataclass
class Suggestion:
    id: str
    draft: StructureDraft
    ts: str
    status: str
    cooldown_until: str | None = None


class DomainRegistry(Protocol):
    def ensure_self(self) -> Domain: ...
    def get_domain(self, domain_id: str) -> Domain: ...
    def find_domain_by_name(self, name: str) -> Domain | None: ...
    def list_domains(self, *, include_archived: bool = False) -> list[Domain]: ...
    def create_domain(self, draft: StructureDraft, *, confirmed: bool) -> Domain: ...
    def get_project(self, project_id: str) -> Project: ...
    def find_project_by_name(self, name: str) -> Project | None: ...
    def list_projects(self, *, include_archived: bool = False) -> list[Project]: ...
    def create_project(self, draft: StructureDraft, *, confirmed: bool) -> Project: ...
    def get_workspace(self, workspace_id: str) -> Workspace: ...
    def list_workspaces(self, *, include_archived: bool = False) -> list[Workspace]: ...
    def create_workspace(self, project_id: str, name: str) -> Workspace: ...
    def rename(self, kind: str, item_id: str, name: str) -> None: ...
    def archive(self, kind: str, item_id: str) -> None: ...
    def record_suggestion(self, draft: StructureDraft) -> Suggestion: ...
    def decline_suggestion(self, suggestion_id: str) -> Suggestion: ...
    def confirm_suggestion(self, suggestion_id: str) -> Domain | Project: ...
    def list_suggestions(self) -> list[Suggestion]: ...
    def enablement(self, domain_id: str) -> list[str]: ...
    def set_enablement(self, domain_id: str, capability_names: list[str]) -> None: ...
    def suggestion_on_cooldown(self, key: str) -> bool: ...

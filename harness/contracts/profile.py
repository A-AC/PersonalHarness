from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Persona:
    version: int
    name: str
    purpose: str
    communication_style: str
    operating_principles: list[str]
    updated_at: str


@dataclass
class PersonaDraft:
    name: str
    purpose: str
    communication_style: str
    operating_principles: list[str]
    reason: str


@dataclass
class PersonaProposal:
    id: str
    draft: PersonaDraft
    ts: str
    status: str
    actor: str


@dataclass
class PersonaVersion:
    version: int
    persona: Persona
    actor: str
    ts: str
    reason: str = ""


class SelfProfile(Protocol):
    def current(self) -> Persona: ...
    def propose_edit(self, draft: PersonaDraft, actor: str) -> PersonaProposal: ...
    def approve(self, proposal_id: str, actor: str) -> Persona: ...
    def reject(self, proposal_id: str, actor: str) -> PersonaProposal: ...
    def history(self) -> list[PersonaVersion]: ...
    def pending(self) -> list[PersonaProposal]: ...

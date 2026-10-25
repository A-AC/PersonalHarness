from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from harness.contracts.model import Message
from harness.contracts.session import Session
from harness.contracts.tasks import Task


@dataclass
class ContextSection:
    name: str
    text: str
    tokens: int


@dataclass
class InclusionRecord:
    item: str
    reason: str
    section: str


@dataclass
class ContextPackage:
    sections: list[ContextSection]
    messages: list[Message]
    total_tokens: int
    included: list[InclusionRecord] = field(default_factory=list)
    excluded: list[InclusionRecord] = field(default_factory=list)
    stats: dict[str, object] = field(default_factory=dict)


class ContextBuilder(Protocol):
    def build(self, task: Task, session: Session, *, trace_id: str | None = None) -> ContextPackage: ...

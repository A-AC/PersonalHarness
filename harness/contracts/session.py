from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harness.contracts.model import Message


@dataclass
class Session:
    id: str
    created_at: str
    updated_at: str
    subject_hits: dict[str, int]


@dataclass
class Interaction:
    id: str
    session_id: str
    ts: str
    user_text: str
    assistant_text: str | None = None
    assistant_ts: str | None = None
    task_ids: list[str] | None = None


class SessionManager(Protocol):
    def resume(self) -> Session: ...
    def current(self) -> Session: ...
    def record_interaction(self, user_text: str, *, ts: str | None = None) -> Interaction: ...
    def append_assistant(
        self,
        interaction_id: str,
        text: str,
        task_ids: list[str] | None = None,
        *,
        ts: str | None = None,
    ) -> None: ...
    def recent_history(self) -> list[Message]: ...
    def get_interaction(self, interaction_id: str) -> Interaction | None: ...
    def list_interactions(self, limit: int | None = 50) -> list[Interaction]: ...
    def bump_subject(self, subject_key: str) -> int: ...
    def cooldown_until(self, key: str) -> str | None: ...
    def set_cooldown(self, key: str, until_ts: str) -> None: ...

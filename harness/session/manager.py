from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from harness.contracts.events import Event, EventLog
from harness.contracts.model import Message
from harness.contracts.session import Interaction, Session
from harness.contracts.settings import SettingsService
from harness.storage.atomic import append_jsonl, atomic_write_json, new_id, now_ts, read_json, read_jsonl


class FileSessionManager:
    def __init__(self, directory: Path, settings: SettingsService, events: EventLog):
        self._dir = directory
        self._settings = settings
        self._events = events
        self._session_path = directory / "session.json"
        self._interactions_path = directory / "interactions.jsonl"
        self._meta_path = directory / "session_meta.json"
        directory.mkdir(parents=True, exist_ok=True)
        self._session = self.resume()

    def _rw(self) -> tuple[int, float]:
        return int(self._settings.get("storage.atomic_retries")), float(self._settings.get("storage.atomic_retry_s"))

    def resume(self) -> Session:
        if self._session_path.exists():
            data = read_json(self._session_path)
            session = Session(
                id=data["id"],
                created_at=data["created_at"],
                updated_at=now_ts(),
                subject_hits=data.get("subject_hits") or {},
            )
        else:
            ts = now_ts()
            session = Session(id=new_id("ses"), created_at=ts, updated_at=ts, subject_hits={})
        self._write_session(session)
        self._session = session
        if not self._meta_path.exists():
            atomic_write_json(self._meta_path, {"cooldowns": {}}, retries=self._rw()[0], retry_s=self._rw()[1])
        return session

    def current(self) -> Session:
        return self._session

    def _write_session(self, session: Session) -> None:
        retries, retry_s = self._rw()
        atomic_write_json(self._session_path, asdict(session), retries=retries, retry_s=retry_s)

    def record_interaction(self, user_text: str) -> Interaction:
        item = Interaction(
            id=new_id("int"),
            session_id=self._session.id,
            ts=now_ts(),
            user_text=user_text,
            assistant_text=None,
            task_ids=[],
        )
        retries, retry_s = self._rw()
        append_jsonl(
            self._interactions_path,
            {**asdict(item), "kind": "user"},
            retries=retries,
            retry_s=retry_s,
        )
        self._session.updated_at = item.ts
        self._write_session(self._session)
        self._events.append(
            Event(id=new_id("evt"), ts=item.ts, actor="user", operation="interaction.record", target=item.id, result="ok")
        )
        return item

    def append_assistant(self, interaction_id: str, text: str, task_ids: list[str] | None = None) -> None:
        retries, retry_s = self._rw()
        append_jsonl(
            self._interactions_path,
            {
                "kind": "assistant_update",
                "id": interaction_id,
                "ts": now_ts(),
                "assistant_text": text,
                "task_ids": list(task_ids or []),
            },
            retries=retries,
            retry_s=retry_s,
        )

    def _merged_interactions(self) -> list[Interaction]:
        merged: dict[str, dict] = {}
        order: list[str] = []
        for row in read_jsonl(self._interactions_path):
            ident = row.get("id")
            if not ident:
                continue
            if row.get("kind") == "assistant_update":
                if ident in merged:
                    merged[ident]["assistant_text"] = row.get("assistant_text")
                    if row.get("task_ids"):
                        merged[ident]["task_ids"] = row["task_ids"]
                continue
            if ident not in merged:
                order.append(ident)
                merged[ident] = {
                    "id": ident,
                    "session_id": row.get("session_id", self._session.id),
                    "ts": row.get("ts", ""),
                    "user_text": row.get("user_text", ""),
                    "assistant_text": row.get("assistant_text"),
                    "task_ids": row.get("task_ids") or [],
                }
        return [Interaction(**merged[ident]) for ident in order]

    def recent_history(self) -> list[Message]:
        window = int(self._settings.get("session.history_window"))
        if window <= 0:
            return []
        messages: list[Message] = []
        for row in self._merged_interactions()[-window:]:
            messages.append(Message(role="user", content=row.user_text))
            if row.assistant_text:
                messages.append(Message(role="assistant", content=row.assistant_text))
        return messages[-window:]

    def get_interaction(self, interaction_id: str) -> Interaction | None:
        for row in self._merged_interactions():
            if row.id == interaction_id:
                return row
        return None

    def list_interactions(self, limit: int = 50) -> list[Interaction]:
        return self._merged_interactions()[-limit:]

    def bump_subject(self, subject_key: str) -> int:
        hits = int(self._session.subject_hits.get(subject_key, 0)) + 1
        self._session.subject_hits[subject_key] = hits
        self._session.updated_at = now_ts()
        self._write_session(self._session)
        return hits

    def cooldown_until(self, key: str) -> str | None:
        data = read_json(self._meta_path) if self._meta_path.exists() else {"cooldowns": {}}
        return (data.get("cooldowns") or {}).get(key)

    def set_cooldown(self, key: str, until_ts: str) -> None:
        data = read_json(self._meta_path) if self._meta_path.exists() else {"cooldowns": {}}
        data.setdefault("cooldowns", {})[key] = until_ts
        retries, retry_s = self._rw()
        atomic_write_json(self._meta_path, data, retries=retries, retry_s=retry_s)

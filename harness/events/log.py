from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness.contracts.events import Event
from harness.contracts.settings import SettingsService
from harness.storage.atomic import append_jsonl, atomic_write_text, new_id, now_ts, read_jsonl
import json


class FileEventLog:
    def __init__(self, directory: Path, settings: SettingsService):
        self._path = directory / "events.jsonl"
        self._settings = settings
        directory.mkdir(parents=True, exist_ok=True)

    def append(self, event: Event) -> Event:
        if not event.id:
            event.id = new_id("evt")
        if not event.ts:
            event.ts = now_ts()
        retries = int(self._settings.get("storage.atomic_retries"))
        retry_s = float(self._settings.get("storage.atomic_retry_s"))
        append_jsonl(self._path, asdict(event), retries=retries, retry_s=retry_s)
        return event

    def query(
        self,
        *,
        actor: str | None = None,
        operation: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> list[Event]:
        rows = self._retained_rows()
        events = [Event(**row) for row in rows]
        if actor:
            events = [item for item in events if item.actor == actor]
        if operation:
            events = [item for item in events if item.operation == operation]
        if since:
            events = [item for item in events if item.ts >= since]
        if until:
            events = [item for item in events if item.ts <= until]
        return events[-limit:]

    def _cutoff(self) -> str:
        days = int(self._settings.get("events.retention_days"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _retained_rows(self) -> list[dict]:
        cutoff = self._cutoff()
        return [row for row in read_jsonl(self._path) if row.get("ts", "") >= cutoff]

    def prune(self) -> None:
        rows = self._retained_rows()
        text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        atomic_write_text(
            self._path,
            text,
            retries=int(self._settings.get("storage.atomic_retries")),
            retry_s=float(self._settings.get("storage.atomic_retry_s")),
        )

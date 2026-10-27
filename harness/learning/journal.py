from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from harness.contracts.events import Event, EventLog
from harness.contracts.learning import JournalEntry
from harness.contracts.settings import SettingsService
from harness.storage.atomic import atomic_write_json, new_id, now_ts, read_json


class FileLearningJournal:
    """File-backed traceable record of every learning decision.

    One JSON file per entry under ``<data>/learning/journal``. Entries are
    append-only in practice: the pipeline is the only writer, and a rollback
    of the underlying change (memory retraction, skill rollback) is recorded
    as a new entry that references the original, never as a rewrite.
    """

    def __init__(self, directory: Path, settings: SettingsService, events: EventLog):
        self._dir = directory
        self._settings = settings
        self._events = events
        directory.mkdir(parents=True, exist_ok=True)

    def append(self, entry: JournalEntry) -> JournalEntry:
        if not entry.id:
            entry.id = new_id("lrn")
        if not entry.ts:
            entry.ts = now_ts()
        retries = int(self._settings.get("storage.atomic_retries"))
        retry_s = float(self._settings.get("storage.atomic_retry_s"))
        atomic_write_json(self._dir / f"{entry.id}.json", asdict(entry), retries=retries, retry_s=retry_s)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=entry.ts,
                actor=entry.actor,
                operation="learning.record",
                target=entry.id,
                result=entry.decision,
                refs=list(entry.refs),
                data={
                    "signal_kind": entry.signal_kind,
                    "category": entry.category,
                    "source_refs": list(entry.source_refs),
                },
            )
        )
        return entry

    def get(self, entry_id: str) -> JournalEntry:
        path = self._dir / f"{entry_id}.json"
        if not path.exists():
            raise KeyError(entry_id)
        return JournalEntry(**read_json(path))

    def list(
        self,
        *,
        limit: int = 100,
        decision: str | None = None,
        kind: str | None = None,
    ) -> list[JournalEntry]:
        items: list[JournalEntry] = []
        for path in sorted(self._dir.glob("lrn_*.json")):
            entry = JournalEntry(**read_json(path))
            if decision and entry.decision != decision:
                continue
            if kind and entry.signal_kind != kind:
                continue
            items.append(entry)
        items.sort(key=lambda item: (item.ts, item.id))
        return items[-limit:]

    def reset(self) -> None:
        for path in self._dir.glob("lrn_*.json"):
            path.unlink(missing_ok=True)

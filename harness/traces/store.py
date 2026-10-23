from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness.contracts.settings import SettingsService
from harness.contracts.traces import Trace
from harness.storage.atomic import atomic_write_json, read_json


class FileTraceStore:
    def __init__(self, directory: Path, settings: SettingsService):
        self._dir = directory
        self._settings = settings
        directory.mkdir(parents=True, exist_ok=True)

    def put(self, trace: Trace) -> None:
        retries = int(self._settings.get("storage.atomic_retries"))
        retry_s = float(self._settings.get("storage.atomic_retry_s"))
        atomic_write_json(self._dir / f"{trace.id}.json", asdict(trace), retries=retries, retry_s=retry_s)

    def get(self, trace_id: str) -> Trace | None:
        path = self._dir / f"{trace_id}.json"
        if not path.exists():
            return None
        return Trace(**read_json(path))

    def list_for(
        self,
        *,
        interaction_id: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[Trace]:
        items: list[Trace] = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(self._settings.get("traces.retention_days")))
        cutoff_s = cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for path in sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            if path.name.endswith(".tmp"):
                continue
            data = read_json(path)
            if data.get("ts", "") < cutoff_s:
                continue
            if interaction_id and data.get("interaction_id") != interaction_id:
                continue
            if task_id and data.get("task_id") != task_id:
                continue
            items.append(Trace(**data))
        return items[-limit:]

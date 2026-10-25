from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from harness.contracts.history import HistorySegment
from harness.contracts.settings import SettingsService
from harness.storage.atomic import atomic_write_json, read_json


class FileHistoryStore:
    """One small JSON file per conversation segment summary."""

    def __init__(self, directory: Path, settings: SettingsService):
        self._dir = directory
        self._settings = settings
        directory.mkdir(parents=True, exist_ok=True)

    def _rw(self) -> tuple[int, float]:
        return (
            int(self._settings.get("storage.atomic_retries")),
            float(self._settings.get("storage.atomic_retry_s")),
        )

    def _path(self, segment_id: str) -> Path:
        return self._dir / f"{segment_id}.json"

    def get(self, segment_id: str) -> HistorySegment | None:
        path = self._path(segment_id)
        if not path.exists():
            return None
        return HistorySegment(**read_json(path))

    def put(self, segment: HistorySegment) -> None:
        retries, retry_s = self._rw()
        atomic_write_json(self._path(segment.id), asdict(segment), retries=retries, retry_s=retry_s)

    def list(self) -> list[HistorySegment]:
        items = [HistorySegment(**read_json(path)) for path in sorted(self._dir.glob("seg_*.json"))]
        return sorted(items, key=lambda item: (item.started_at, item.id))

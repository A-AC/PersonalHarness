from __future__ import annotations

import json
import shutil
import threading
import time
import zipfile
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager

from harness.contracts.domains import DomainRegistry
from harness.contracts.events import Event, EventLog
from harness.contracts.maintenance import RemovedState, ResetReport
from harness.contracts.memory import MemoryManager
from harness.contracts.profile import SelfProfile
from harness.contracts.session import SessionManager
from harness.contracts.settings import SettingsService
from harness.storage.atomic import (
    atomic_write_text,
    new_id,
    now_ts,
    read_json,
    read_jsonl,
)

Gate = Callable[[], ContextManager[Any]]


class FileResetService:
    """Central reset for all learned state under one data directory.

    Reset is a maintenance boundary, not a manager: it only depends on manager
    contracts, the shared storage helpers, and gates supplied by the composition
    root. A gate is a context-manager factory that quiesces one component (the
    turn service, history summary worker, consolidation worker, ...).
    """

    SCOPES = ("full", "memories", "conversations")

    # Structural data layout, relative to the data directory. These are paths,
    # not tuning values; the categories are exposed through ``scopes``.
    _PATHS = {
        "memories": (
            "memory",
            "consolidation",
        ),
        "conversations": (
            "sessions",
            "traces",
            "history",
            "events",
            "capabilities/attempts",
            "tasks",
            "consolidation",
        ),
        "full": (
            "settings.yaml",
            "settings_history.jsonl",
            "sessions",
            "traces",
            "history",
            "events",
            "registry",
            "tasks",
            "persona",
            "capabilities/attempts",
            "memory",
            "consolidation",
        ),
    }

    # Archive these sibling trees only when the user explicitly includes them;
    # they are measurement artifacts, not assistant runtime state.
    _ARCHIVE_SKIP = {"backups", "eval", "experiments", "__pycache__"}

    def __init__(
        self,
        data_dir: Path,
        *,
        settings: SettingsService,
        sessions: SessionManager,
        profile: SelfProfile,
        domains: DomainRegistry,
        memory: MemoryManager,
        events: EventLog,
        gates: list[Gate] | None = None,
    ):
        self._root = Path(data_dir)
        self._settings = settings
        self._sessions = sessions
        self._profile = profile
        self._domains = domains
        self._memory = memory
        self._events = events
        self._gates = list(gates or [])
        self._lock = threading.Lock()
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def scopes(self) -> tuple[str, ...]:
        return self.SCOPES

    def run(
        self,
        scope: str = "full",
        *,
        actor: str = "user",
        backup: bool | None = None,
    ) -> ResetReport:
        chosen = (scope or "").strip().lower()
        if chosen not in self.SCOPES:
            raise ValueError(f"unknown reset scope: {scope}")
        started = time.monotonic()
        with self._lock:
            self._busy = True
            try:
                with ExitStack() as stack:
                    for gate in self._gates:
                        stack.enter_context(gate())
                    use_backup = (
                        bool(self._settings.get("maintenance.reset.backup"))
                        if backup is None
                        else bool(backup)
                    )
                    backup_path = self._archive(chosen) if use_backup else None
                    removed = self._wipe(chosen)
                    seeded = self._seed(chosen)
                    cleared = 0
                    if chosen == "memories":
                        cleared = self._scrub_memory_references()
                        self._memory.reset()
                    elif chosen == "conversations":
                        self._memory.reset(keep_records=True)
                    else:
                        self._memory.reset()
                    report = ResetReport(
                        scope=chosen,
                        removed=removed,
                        backup_path=backup_path,
                        seeded=seeded,
                        cleared_references=cleared,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                    self._record_event(report, actor=actor)
                    return report
            finally:
                self._busy = False

    # --------------------------------------------------------------- archive
    def _archive(self, scope: str) -> str:
        destination = self._root / "backups"
        destination.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive = destination / f"reset_{scope}_{stamp}.zip"
        entries: list[str] = []
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(self._root.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(self._root)
                if relative.parts and relative.parts[0] in self._ARCHIVE_SKIP:
                    continue
                if path.suffix == ".tmp":
                    continue
                bundle.write(path, relative.as_posix())
                entries.append(relative.as_posix())
            bundle.writestr(
                "_reset_manifest.json",
                json.dumps(
                    {
                        "scope": scope,
                        "created_at": now_ts(),
                        "data_dir": str(self._root),
                        "entries": entries,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
        return str(archive)

    # ------------------------------------------------------------------ wipe
    def _wipe(self, scope: str) -> list[RemovedState]:
        removed: list[RemovedState] = []
        for relative in self._PATHS[scope]:
            path = self._root / relative
            if not path.exists():
                continue
            files, size = self._count(path)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            removed.append(RemovedState(path=relative, files=files, bytes=size))
        return removed

    @staticmethod
    def _count(path: Path) -> tuple[int, int]:
        if path.is_file():
            return 1, path.stat().st_size
        files = 0
        size = 0
        for item in path.rglob("*"):
            if item.is_file():
                files += 1
                size += item.stat().st_size
        return files, size

    # ------------------------------------------------------------------ seed
    def _seed(self, scope: str) -> list[str]:
        seeded: list[str] = []
        if scope == "full":
            self._settings.reload()
            seeded.append("default settings")
        if scope in {"full", "conversations"}:
            self._sessions.resume()
            seeded.append("new session")
        if scope == "full":
            persona = self._profile.reset()
            seeded.append(f"persona v{persona.version}")
            domain = self._domains.ensure_self()
            seeded.append(f"domain {domain.id}")
        return seeded

    # --------------------------------------------------------------- cleanup
    def _scrub_memory_references(self) -> int:
        """Remove memory ids from retained traces/attempts/events."""
        cleared = 0
        for folder in ("traces",):
            directory = self._root / folder
            for path in sorted(directory.glob("*.json")):
                data = read_json(path)
                updated, count = self._strip_memory_refs(data)
                if count:
                    self._write_json(path, updated)
                    cleared += count

        attempts = self._root / "capabilities" / "attempts"
        for path in sorted(attempts.glob("op_*.json")):
            data = read_json(path)
            if data.get("capability") != "harness_memory":
                continue
            path.unlink(missing_ok=True)
            cleared += 1

        event_path = self._root / "events" / "events.jsonl"
        if event_path.exists():
            kept: list[dict[str, Any]] = []
            for row in read_jsonl(event_path):
                if self._is_memory_event(row):
                    cleared += 1
                    continue
                kept.append(row)
            self._write_jsonl(event_path, kept)
        return cleared

    @classmethod
    def _strip_memory_refs(cls, value: Any) -> tuple[Any, int]:
        cleared = 0
        if isinstance(value, dict):
            updated: dict[str, Any] = {}
            for key, item in value.items():
                if key in {"memory_id", "winner_id", "loser_id", "supersedes", "superseded_by"}:
                    if isinstance(item, str) and cls._is_memory_id(item):
                        cleared += 1
                        continue
                if key == "item" and isinstance(item, str) and cls._is_memory_id(item):
                    cleared += 1
                    continue
                if key == "run_id" and isinstance(item, str) and item.startswith("run_"):
                    cleared += 1
                    continue
                if key == "restore_ids" and isinstance(item, list):
                    kept = [entry for entry in item if not cls._is_memory_id(str(entry))]
                    cleared += len(item) - len(kept)
                    updated[key] = kept
                    continue
                if key == "replaced" and isinstance(item, list):
                    kept = [
                        entry
                        for entry in item
                        if not (
                            isinstance(entry, dict)
                            and cls._is_memory_id(str(entry.get("id") or ""))
                        )
                    ]
                    cleared += len(item) - len(kept)
                    updated[key] = kept
                    continue
                updated[key], nested = cls._strip_memory_refs(item)
                cleared += nested
            return updated, cleared
        if isinstance(value, list):
            items: list[Any] = []
            for item in value:
                stripped, nested = cls._strip_memory_refs(item)
                items.append(stripped)
                cleared += nested
            return items, cleared
        if isinstance(value, str) and value.startswith(("included:", "excluded:")):
            return value, cleared
        return value, cleared

    @classmethod
    def _is_memory_event(cls, row: dict[str, Any]) -> bool:
        operation = str(row.get("operation") or "")
        target = str(row.get("target") or "")
        refs = [str(item) for item in row.get("refs") or []]
        if operation.startswith(("memory.", "consolidation.")):
            return True
        if cls._is_memory_id(target):
            return True
        return any(cls._is_memory_id(item) or item.startswith("run_") for item in refs)

    @staticmethod
    def _is_memory_id(value: str) -> bool:
        return value.startswith("mem_") or value.startswith("run_")

    def _write_json(self, path: Path, data: Any) -> None:
        retries = int(self._settings.get("storage.atomic_retries"))
        retry_s = float(self._settings.get("storage.atomic_retry_s"))
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        atomic_write_text(path, text, retries=retries, retry_s=retry_s)

    def _write_jsonl(self, path: Path, rows: list[dict[str, Any]]) -> None:
        retries = int(self._settings.get("storage.atomic_retries"))
        retry_s = float(self._settings.get("storage.atomic_retry_s"))
        text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        atomic_write_text(path, text, retries=retries, retry_s=retry_s)

    def _record_event(self, report: ResetReport, *, actor: str) -> None:
        try:
            self._events.append(
                Event(
                    id=new_id("evt"),
                    ts=now_ts(),
                    actor=actor,
                    operation="reset.run",
                    target=report.scope,
                    result="ok",
                    data={
                        "removed": [asdict(item) for item in report.removed],
                        "backup_path": report.backup_path,
                        "seeded": report.seeded,
                        "cleared_references": report.cleared_references,
                        "duration_ms": report.duration_ms,
                    },
                )
            )
        except Exception:
            # The reset itself succeeded; an audit append must not turn it into
            # a failure the caller might retry.
            pass

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

from harness.constants import SkillStatus
from harness.contracts.events import Event, EventLog
from harness.contracts.learning import SkillDraft, SkillRecord, SkillVersion
from harness.contracts.settings import SettingsService
from harness.learning.strategy import check_skill_text
from harness.storage.atomic import (
    assert_safe_filename,
    atomic_write_json,
    new_id,
    now_ts,
    read_json,
)
from harness.text import name_overlap, normalize_name

SKILL_FILENAME = "SKILL.md"

# A skill name is also a directory name: keep it slug-shaped.
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,48}$")


class SkillValidationError(ValueError):
    """Raised when a skill draft or transition fails validation."""


class FileSkillLibrary:
    """Controlled lifecycle for learned skills.

    Layout under the library root:

    - ``records/skl_*.json`` — one metadata record per skill (status,
      provenance, test evidence, version history).
    - ``candidates/<name>/SKILL.md`` — staged candidate text. The executor
      never reads this directory, so an untested candidate cannot reach the
      prompt.
    - ``active/<name>/SKILL.md`` — approved text and the executor's
      discovery directory.

    Guarantees:

    - A candidate never replaces a trusted (approved) skill: approval of a
      replacement requires harness-verified test evidence, and the previous
      version is kept for rollback.
    - Skill text is safety-scanned at proposal; text that weakens a safety
      invariant is rejected outright.
    - Overlapping names are rejected so trivial variations do not pile up.
    - ``on_change`` (wired by the composition root to the executor's skill
      reload) keeps prompt disclosure in sync with the library.
    """

    def __init__(
        self,
        root: Path,
        settings: SettingsService,
        events: EventLog,
        *,
        on_change: Callable[[], None] | None = None,
    ):
        self._root = root
        self._records_dir = root / "records"
        self._candidates_dir = root / "candidates"
        self._active_dir = root / "active"
        self._settings = settings
        self._events = events
        self._on_change = on_change
        for directory in (self._records_dir, self._candidates_dir, self._active_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def active_dir(self) -> Path:
        return self._active_dir

    def attach_on_change(self, on_change: Callable[[], None]) -> None:
        """Set the disclosure-refresh callback (wired by the composition root)."""
        self._on_change = on_change

    # ------------------------------------------------------------- lifecycle
    def propose(self, draft: SkillDraft, *, actor: str) -> SkillRecord:
        name = str(draft.name or "").strip()
        if not SKILL_NAME_RE.match(name):
            raise SkillValidationError(
                f"skill name '{name}' must be a short lowercase slug (letters, digits, -, _)"
            )
        assert_safe_filename(name)
        description = str(draft.description or "").strip()
        body = str(draft.body or "").strip()
        if not description or not body:
            raise SkillValidationError("a skill needs a description and a body")
        violations = check_skill_text(name, description, body)
        if violations:
            raise SkillValidationError(
                "skill text violates safety invariants: " + "; ".join(violations)
            )
        preconditions = self._clean_preconditions(draft.preconditions)
        same_name = [item for item in self.list() if normalize_name(item.name) == normalize_name(name)]
        if any(item.status == SkillStatus.CANDIDATE for item in same_name):
            raise SkillValidationError(f"a candidate skill '{name}' already exists")
        approved = [item for item in same_name if item.status == SkillStatus.APPROVED]
        if approved and draft.supersedes != approved[0].id:
            raise SkillValidationError(
                f"skill '{name}' is approved; an improvement must declare supersedes={approved[0].id}"
            )
        if not same_name:
            overlap_threshold = float(self._settings.get("learning.skill_name_overlap"))
            for other in self.list():
                if other.status in {SkillStatus.CANDIDATE, SkillStatus.APPROVED} and name_overlap(
                    name, other.name
                ) >= overlap_threshold:
                    raise SkillValidationError(
                        f"skill '{name}' overlaps existing skill '{other.name}'; "
                        "improve that skill instead of creating a variation"
                    )
        if draft.supersedes:
            target = self.get(draft.supersedes)
            if target.status != SkillStatus.APPROVED:
                raise SkillValidationError(
                    f"supersedes target {draft.supersedes} is not an approved skill"
                )
            if normalize_name(target.name) != normalize_name(name):
                raise SkillValidationError("an improvement must keep the same skill name")

        ts = now_ts()
        record = SkillRecord(
            id=new_id("skl"),
            name=name,
            description=description,
            preconditions=list(preconditions),
            status=SkillStatus.CANDIDATE.value,
            created_at=ts,
            updated_at=ts,
            actor=actor,
            provenance=dict(draft.provenance),
            test_evidence=None,
            supersedes=draft.supersedes or None,
            versions=[
                SkillVersion(
                    version=1,
                    body=body,
                    preconditions=list(preconditions),
                    description=description,
                    ts=ts,
                    actor=actor,
                    note=draft.reason or "proposed",
                )
            ],
        )
        self._write_record(record)
        self._write_text(self._candidates_dir / name / SKILL_FILENAME, self._markdown(record))
        self._event("skill.propose", record, "candidate")
        return record

    def record_test(self, skill_id: str, evidence: dict[str, Any], *, actor: str) -> SkillRecord:
        record = self.get(skill_id)
        if record.status != SkillStatus.CANDIDATE:
            raise SkillValidationError("only a candidate skill can receive test evidence")
        payload = dict(evidence or {})
        payload.setdefault("recorded_at", now_ts())
        payload.setdefault("recorded_by", actor)
        record = replace(record, test_evidence=payload, updated_at=now_ts())
        self._write_record(record)
        self._event("skill.test", record, "recorded", data={"passed": bool(payload.get("passed"))})
        return record

    def approve(self, skill_id: str, *, actor: str) -> SkillRecord:
        record = self.get(skill_id)
        if record.status != SkillStatus.CANDIDATE:
            raise SkillValidationError(f"skill {skill_id} is {record.status}, not a candidate")
        if record.supersedes:
            target = self.get(record.supersedes)
            if target.status != SkillStatus.APPROVED:
                raise SkillValidationError(
                    f"supersedes target {target.id} is {target.status}; cannot take over"
                )
            evidence = record.test_evidence or {}
            if not evidence.get("passed"):
                raise SkillValidationError(
                    "an untested candidate cannot replace a trusted skill: "
                    "record passing test evidence first"
                )
            # Retire the trusted skill, keeping its text for rollback.
            retired = replace(target, status=SkillStatus.RETIRED.value, updated_at=now_ts())
            self._write_record(retired)
            self._remove_dir(self._active_dir / retired.name)
            self._event("skill.retire", retired, "superseded")
        record = replace(record, status=SkillStatus.APPROVED.value, updated_at=now_ts())
        self._write_record(record)
        self._remove_dir(self._candidates_dir / record.name)
        self._write_text(self._active_dir / record.name / SKILL_FILENAME, self._markdown(record))
        self._event("skill.approve", record, "approved", data={"tested": bool((record.test_evidence or {}).get("passed"))})
        self._notify_change()
        return record

    def reject(self, skill_id: str, *, actor: str) -> SkillRecord:
        record = self.get(skill_id)
        if record.status != SkillStatus.CANDIDATE:
            raise SkillValidationError(f"skill {skill_id} is {record.status}, not a candidate")
        record = replace(record, status=SkillStatus.REJECTED.value, updated_at=now_ts())
        self._write_record(record)
        self._remove_dir(self._candidates_dir / record.name)
        self._event("skill.reject", record, "rejected")
        return record

    def retire(self, skill_id: str, *, actor: str) -> SkillRecord:
        record = self.get(skill_id)
        if record.status != SkillStatus.APPROVED:
            raise SkillValidationError(f"skill {skill_id} is {record.status}, not approved")
        record = replace(record, status=SkillStatus.RETIRED.value, updated_at=now_ts())
        self._write_record(record)
        self._remove_dir(self._active_dir / record.name)
        self._event("skill.retire", record, "retired")
        self._notify_change()
        return record

    def rollback(self, skill_id: str, *, actor: str) -> SkillRecord:
        """Undo an approval: restore the skill version the record replaced."""
        record = self.get(skill_id)
        if record.status != SkillStatus.APPROVED or not record.supersedes:
            raise SkillValidationError("only an approved replacement can be rolled back")
        previous = self.get(record.supersedes)
        if previous.status != SkillStatus.RETIRED:
            raise SkillValidationError(
                f"skill {previous.id} is {previous.status}; nothing to restore"
            )
        # Remove the replaced text before restoring: an improvement keeps the
        # same name, so the write must not be undone by the removal.
        self._remove_dir(self._active_dir / record.name)
        retired = replace(record, status=SkillStatus.RETIRED.value, updated_at=now_ts())
        self._write_record(retired)
        restored = replace(previous, status=SkillStatus.APPROVED.value, updated_at=now_ts())
        self._write_record(restored)
        self._write_text(self._active_dir / restored.name / SKILL_FILENAME, self._markdown(restored))
        self._event("skill.rollback", retired, "restored", refs=[restored.id])
        self._notify_change()
        return retired

    # --------------------------------------------------------------- queries
    def get(self, skill_id: str) -> SkillRecord:
        path = self._records_dir / f"{skill_id}.json"
        if not path.exists():
            raise KeyError(skill_id)
        return self._load(path)

    def get_by_name(self, name: str) -> SkillRecord | None:
        normalized = normalize_name(name)
        matches = [item for item in self.list() if normalize_name(item.name) == normalized]
        if not matches:
            return None
        # Prefer the live record: an approved skill (or its pending
        # improvement candidate) over retired/rejected history.
        for status in (
            SkillStatus.APPROVED.value,
            SkillStatus.CANDIDATE.value,
            SkillStatus.RETIRED.value,
            SkillStatus.REJECTED.value,
        ):
            for item in matches:
                if item.status == status:
                    return item
        return matches[0]

    def list(self, *, status: str | None = None) -> list[SkillRecord]:
        records = [self._load(path) for path in sorted(self._records_dir.glob("skl_*.json"))]
        if status:
            records = [item for item in records if item.status == status]
        records.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return records

    def drop_candidates(self) -> int:
        """Remove every pending candidate and its staged text.

        Used by the conversations reset: candidates cite conversation and
        attempt evidence that the wipe removes, so an unapproved proposal
        with unverifiable evidence must not survive it.
        """
        removed = 0
        for record in self.list(status=SkillStatus.CANDIDATE.value):
            self._remove_dir(self._candidates_dir / record.name)
            (self._records_dir / f"{record.id}.json").unlink(missing_ok=True)
            removed += 1
        return removed

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _load(path: Path) -> SkillRecord:
        data = read_json(path)
        versions = [SkillVersion(**item) for item in data.get("versions") or []]
        return SkillRecord(**{**data, "versions": versions})

    def _write_record(self, record: SkillRecord) -> None:
        retries = int(self._settings.get("storage.atomic_retries"))
        retry_s = float(self._settings.get("storage.atomic_retry_s"))
        atomic_write_json(self._records_dir / f"{record.id}.json", asdict(record), retries=retries, retry_s=retry_s)

    def _write_text(self, path: Path, text: str) -> None:
        """Atomic SKILL.md write.

        ``SKILL.md`` is a fixed filename inside a slug-validated folder, so
        the storage helper's id-oriented filename assert does not apply; the
        write is still atomic (temp file + replace) with the same retry
        policy as the shared helpers.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        attempts = max(1, int(self._settings.get("storage.atomic_retries")))
        delay = float(self._settings.get("storage.atomic_retry_s"))
        last_error: OSError | None = None
        for _ in range(attempts):
            try:
                os.replace(tmp, path)
                return
            except OSError as exc:
                last_error = exc
                time.sleep(delay)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise last_error or OSError("atomic replace failed")

    @staticmethod
    def _remove_dir(path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _clean_preconditions(items: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in items or []:
            for token in re.split(r"[,|]", str(item)):
                token = token.strip().lower()
                if token and token not in cleaned:
                    cleaned.append(token)
        return cleaned

    @staticmethod
    def _markdown(record: SkillRecord) -> str:
        """Render the record's current version as the on-disk SKILL.md.

        The front matter is the only part the discovery loader parses; the
        body stays human/machine reviewable text.
        """
        version = record.versions[-1] if record.versions else None
        body = version.body if version else ""
        description = version.description if version else record.description
        preconditions = record.preconditions
        header = "---\n"
        header += f"name: {record.name}\n"
        header += f"description: {description}\n"
        if preconditions:
            header += f"preconditions: {', '.join(preconditions)}\n"
        header += "---\n\n"
        return header + body.strip() + "\n"

    def _event(self, operation: str, record: SkillRecord, result: str, *, refs: list[str] | None = None, data: dict[str, Any] | None = None) -> None:
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=now_ts(),
                actor="harness",
                operation=operation,
                target=record.id,
                result=result,
                refs=list(refs or []),
                data={"name": record.name, **(data or {})},
            )
        )

    def _notify_change(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:
            # Disclosure refresh is best effort; the library remains the
            # source of truth and the next reload picks the change up.
            pass

from __future__ import annotations

from dataclasses import asdict, fields
from pathlib import Path

from harness.constants import DecisionStatus
from harness.contracts.decisions import Decision
from harness.contracts.domains import Suggestion
from harness.contracts.events import Event, EventLog
from harness.contracts.settings import SettingsService
from harness.storage.atomic import atomic_write_json, new_id, now_ts, read_json


OPEN_STATUSES = {DecisionStatus.PENDING.value, DecisionStatus.DEFERRED.value}


class FileDecisionStore:
    """File-backed decision log using the same atomic JSON convention as tasks.

    One decision is one file. The source of truth for *what was proposed* stays
    in the decision record; domain suggestions also remain in the registry so
    existing data remains inspectable and compatible.
    """

    def __init__(self, directory: Path, settings: SettingsService, events: EventLog):
        self._dir = directory
        self._settings = settings
        self._events = events
        directory.mkdir(parents=True, exist_ok=True)

    def _rw(self) -> tuple[int, float]:
        return (
            int(self._settings.get("storage.atomic_retries")),
            float(self._settings.get("storage.atomic_retry_s")),
        )

    def _path(self, decision_id: str) -> Path:
        return self._dir / f"{decision_id}.json"

    def _save(self, decision: Decision) -> None:
        retries, retry_s = self._rw()
        atomic_write_json(self._path(decision.id), asdict(decision), retries=retries, retry_s=retry_s)

    def _load(self, path: Path) -> Decision:
        data = read_json(path)
        known = {item.name for item in fields(Decision)}
        return Decision(**{key: value for key, value in data.items() if key in known})

    def create(self, decision: Decision) -> Decision:
        if not decision.id:
            decision.id = new_id("dec")
        self._save(decision)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=decision.created_at,
                actor="harness",
                operation="decision.create",
                target=decision.id,
                result=decision.status,
                trigger_ref=decision.interaction_id,
                refs=[item for item in [decision.task_id] if item],
                data={
                    "kind": decision.kind,
                    "risk": decision.risk,
                    "summary": decision.summary,
                    "task_id": decision.task_id,
                },
            )
        )
        return decision

    def get(self, decision_id: str) -> Decision:
        path = self._path(decision_id)
        if not path.exists():
            raise KeyError(decision_id)
        return self._load(path)

    def list(self, status: str | None = None, *, include_resolved: bool = True) -> list[Decision]:
        items = [self._load(path) for path in sorted(self._dir.glob("dec_*.json"))]
        if status is not None:
            wanted = str(status)
            items = [item for item in items if item.status == wanted]
        elif not include_resolved:
            items = [item for item in items if item.status in OPEN_STATUSES]
        items.sort(key=lambda item: (item.created_at, item.id))
        return items

    def pending(self, *, include_deferred: bool = True) -> list[Decision]:
        allowed = {DecisionStatus.PENDING.value}
        if include_deferred:
            allowed.add(DecisionStatus.DEFERRED.value)
        return [item for item in self.list() if item.status in allowed]

    def update(self, decision: Decision) -> Decision:
        decision.updated_at = now_ts()
        self._save(decision)
        return decision

    def find_for_task(self, task_id: str, *, open_only: bool = False) -> Decision | None:
        matches = [item for item in self.list() if task_id in item.task_ids]
        if open_only:
            matches = [item for item in matches if item.is_open]
        return matches[-1] if matches else None

    def find_for_suggestion(self, suggestion_id: str) -> Decision | None:
        for item in self.list():
            if str(item.proposal.get("suggestion_id") or "") == suggestion_id:
                return item
        return None

    def sync_suggestion(self, suggestion: Suggestion) -> Decision:
        """Mirror a domain suggestion into the decision log.

        The registry remains the domain source of truth. This mirror makes a
        suggestion resolvable through the same persisted decision path as an
        action confirmation and keeps old suggestion rows compatible.
        """
        existing = self.find_for_suggestion(suggestion.id)
        if existing is None:
            ts = now_ts()
            decision = Decision(
                id=new_id("dec"),
                kind="domain_creation",
                status=DecisionStatus.PENDING.value,
                created_at=ts,
                updated_at=ts,
                prompt=f"Create a domain named '{suggestion.draft.name}'?",
                summary=f"Create domain '{suggestion.draft.name}'",
                proposal={
                    "type": "domain_creation",
                    "suggestion_id": suggestion.id,
                    "name": suggestion.draft.name,
                    "reason": suggestion.draft.reason,
                    "evidence": suggestion.draft.evidence,
                },
                scope={"suggestion_id": suggestion.id},
                risk="write",
                source="harness",
                metadata={"suggestion_id": suggestion.id},
            )
            return self.create(decision)

        target: str | None = None
        resolution: str | None = None
        if suggestion.status == "confirmed" and not existing.status == DecisionStatus.COMPLETED.value:
            target = DecisionStatus.COMPLETED.value
            resolution = "approved"
        elif suggestion.status == "declined" and not existing.status in {
            DecisionStatus.REJECTED.value,
            DecisionStatus.CANCELLED.value,
            DecisionStatus.COMPLETED.value,
        }:
            target = DecisionStatus.REJECTED.value
            resolution = "rejected"
        if target is not None:
            existing.status = target
            existing.resolution = resolution
            existing.resolution_text = (
                f"Domain '{suggestion.draft.name}' was confirmed."
                if target == DecisionStatus.COMPLETED.value
                else f"Domain '{suggestion.draft.name}' was declined."
            )
            existing.resolved_at = now_ts()
            existing.updated_at = existing.resolved_at
            self._save(existing)
        return existing

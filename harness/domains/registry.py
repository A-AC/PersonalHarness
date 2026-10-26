from __future__ import annotations

from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness.constants import SELF_DOMAIN_ID, SELF_DOMAIN_NAME
from harness.contracts.domains import Domain, StructureDraft, Suggestion
from harness.contracts.events import Event, EventLog
from harness.contracts.settings import SettingsService
from harness.storage.atomic import atomic_write_json, new_id, now_ts, read_json


def _row(cls: type, data: dict) -> dict:
    """Drop keys a previous release wrote but the current contract removed."""
    known = {item.name for item in fields(cls)}
    return {key: value for key, value in data.items() if key in known}
from harness.text import name_overlap, normalize_name


class UnconfirmedError(ValueError):
    pass


class FileDomainRegistry:
    def __init__(self, directory: Path, settings: SettingsService, events: EventLog):
        self._dir = directory
        self._settings = settings
        self._events = events
        (directory / "domains").mkdir(parents=True, exist_ok=True)
        (directory / "suggestions").mkdir(parents=True, exist_ok=True)
        self.ensure_self()

    def _rw(self) -> tuple[int, float]:
        return int(self._settings.get("storage.atomic_retries")), float(self._settings.get("storage.atomic_retry_s"))

    def _save(self, folder: str, ident: str, data: dict) -> None:
        retries, retry_s = self._rw()
        atomic_write_json(self._dir / folder / f"{ident}.json", data, retries=retries, retry_s=retry_s)

    def _default_capabilities(self) -> list[str]:
        raw = str(self._settings.get("capabilities.default_enabled") or "")
        return [item.strip() for item in raw.split(",") if item.strip()]

    def ensure_self(self) -> Domain:
        path = self._dir / "domains" / f"{SELF_DOMAIN_ID}.json"
        if path.exists():
            return Domain(**_row(Domain, read_json(path)))
        ts = now_ts()
        domain = Domain(
            id=SELF_DOMAIN_ID,
            name=SELF_DOMAIN_NAME,
            status="active",
            created_at=ts,
            created_by="harness",
            created_reason="reserved assistant domain",
            evidence="spec 6.7",
            capability_names=self._default_capabilities(),
        )
        self._save("domains", domain.id, asdict(domain))
        return domain

    def get_domain(self, domain_id: str) -> Domain:
        path = self._dir / "domains" / f"{domain_id}.json"
        if not path.exists():
            raise KeyError(domain_id)
        return Domain(**_row(Domain, read_json(path)))

    def find_domain_by_name(self, name: str) -> Domain | None:
        wanted = name.strip().lower()
        for domain in self.list_domains(include_archived=True):
            if domain.name.lower() == wanted:
                return domain
        return None

    def list_domains(self, *, include_archived: bool = False) -> list[Domain]:
        items = [
            Domain(**_row(Domain, read_json(path)))
            for path in sorted((self._dir / "domains").glob("*.json"))
        ]
        if not include_archived:
            items = [item for item in items if not item.archived]
        return items

    def create_domain(self, draft: StructureDraft, *, confirmed: bool) -> Domain:
        if not confirmed:
            raise UnconfirmedError("domain creation requires confirmation")
        existing = self.find_domain_by_name(draft.name)
        if existing and not existing.archived:
            return existing
        ts = now_ts()
        ident = new_id("dom")
        names = list(draft.capability_names) or self._default_capabilities()
        domain = Domain(
            id=ident,
            name=draft.name,
            status="active",
            created_at=ts,
            created_by="user",
            created_reason=draft.reason,
            evidence=draft.evidence,
            capability_names=names,
        )
        self._save("domains", domain.id, asdict(domain))
        self._events.append(
            Event(id=new_id("evt"), ts=ts, actor="user", operation="domain.create", target=domain.id, result="ok", data={"name": domain.name, "reason": draft.reason})
        )
        return domain

    def rename(self, item_id: str, name: str) -> None:
        domain = self.get_domain(item_id)
        domain.name = name
        self._save("domains", domain.id, asdict(domain))
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=now_ts(),
                actor="user",
                operation="domain.rename",
                target=domain.id,
                result="ok",
                data={"name": name},
            )
        )

    def archive(self, item_id: str) -> None:
        if item_id == SELF_DOMAIN_ID:
            raise ValueError("cannot archive self")
        domain = self.get_domain(item_id)
        domain.status = "archived"
        domain.archived = True
        self._save("domains", domain.id, asdict(domain))
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=now_ts(),
                actor="user",
                operation="domain.archive",
                target=domain.id,
                result="archived",
            )
        )

    def record_suggestion(self, draft: StructureDraft) -> Suggestion:
        suggestion = Suggestion(id=new_id("sug"), draft=draft, ts=now_ts(), status="pending")
        payload = asdict(suggestion)
        self._save("suggestions", suggestion.id, payload)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=suggestion.ts,
                actor="model",
                operation="domain.suggestion.create",
                target=suggestion.id,
                result="pending",
                data={"name": draft.name, "reason": draft.reason},
            )
        )
        return suggestion

    def decline_suggestion(self, suggestion_id: str) -> Suggestion:
        data = read_json(self._dir / "suggestions" / f"{suggestion_id}.json")
        draft = StructureDraft(**_row(StructureDraft, data["draft"]))
        cooldown_s = int(self._settings.get("domain.suggestion.cooldown_s"))
        until = datetime.now(timezone.utc) + timedelta(seconds=cooldown_s)
        suggestion = Suggestion(
            id=data["id"],
            draft=draft,
            ts=data["ts"],
            status="declined",
            cooldown_until=until.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )
        self._save("suggestions", suggestion.id, asdict(suggestion))
        self._events.append(
            Event(id=new_id("evt"), ts=now_ts(), actor="user", operation="domain.suggestion.decline", target=suggestion.id, result="declined")
        )
        return suggestion

    def confirm_suggestion(self, suggestion_id: str) -> Domain:
        data = read_json(self._dir / "suggestions" / f"{suggestion_id}.json")
        draft = StructureDraft(**_row(StructureDraft, data["draft"]))
        created = self.create_domain(draft, confirmed=True)
        data["status"] = "confirmed"
        self._save("suggestions", data["id"], data)
        return created

    def list_suggestions(self) -> list[Suggestion]:
        items = []
        for path in sorted((self._dir / "suggestions").glob("*.json")):
            data = read_json(path)
            data["draft"] = StructureDraft(**_row(StructureDraft, data["draft"]))
            items.append(Suggestion(**_row(Suggestion, data)))
        return items

    def enablement(self, domain_id: str) -> list[str]:
        return list(self.get_domain(domain_id).capability_names)

    def set_enablement(self, domain_id: str, capability_names: list[str]) -> None:
        domain = self.get_domain(domain_id)
        domain.capability_names = list(capability_names)
        self._save("domains", domain.id, asdict(domain))

    def suggestion_on_cooldown(self, key: str) -> bool:
        now = now_ts()
        threshold = float(self._settings.get("domain.match_overlap"))
        wanted = normalize_name(key)
        for suggestion in self.list_suggestions():
            if suggestion.status != "declined" or not suggestion.cooldown_until:
                continue
            if suggestion.cooldown_until <= now:
                continue
            name = suggestion.draft.name
            if normalize_name(name) == wanted or name_overlap(name, key) >= threshold:
                return True
        return False

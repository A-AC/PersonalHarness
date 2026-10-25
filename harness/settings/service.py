from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from harness.contracts.events import Event, EventLog
from harness.contracts.settings import SettingChange, SettingDef, SettingsError
from harness.settings.catalog import RETIRED_SETTINGS, catalog
from harness.storage.atomic import atomic_write_text, new_id, now_ts, read_jsonl, append_jsonl


__all__ = ["FileSettingsService", "SettingsError"]


def _dot_get(data: dict[str, Any], key: str) -> Any:
    cur: Any = data
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(key)
        cur = cur[part]
    return cur


def _dot_set(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _flatten_keys(data: dict[str, Any], prefix: str = "") -> list[str]:
    keys: list[str] = []
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            keys.extend(_flatten_keys(value, full))
        else:
            keys.append(full)
    return keys


def _dot_delete(data: dict[str, Any], key: str) -> None:
    parts = key.split(".")
    cur = data
    stack: list[tuple[dict[str, Any], str]] = []
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            return
        stack.append((cur, part))
        cur = cur[part]
    cur.pop(parts[-1], None)


class FileSettingsService:
    def __init__(self, path: Path, *, events: EventLog | None = None, env: dict[str, str] | None = None):
        self._path = path
        self._events = events
        self._defs = {item.key: item for item in catalog()}
        self._values: dict[str, Any] = {}
        self._history_path = path.with_name("settings_history.jsonl")
        self._env = dict(env or {})
        self._load()
        self._apply_env(self._env)

    def attach_events(self, events: EventLog) -> None:
        self._events = events

    def _retries(self) -> tuple[int, float]:
        return (
            int(self._values.get("storage.atomic_retries", 8)),
            float(self._values.get("storage.atomic_retry_s", 0.05)),
        )

    def _load(self) -> None:
        defaults = {item.key: deepcopy(item.default) for item in self._defs.values()}
        raw: dict[str, Any] = {}
        if self._path.exists():
            loaded = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise SettingsError("settings file must be a mapping")
            raw = loaded
        dropped_retired = False
        for key in _flatten_keys(raw):
            if key not in self._defs:
                if key in RETIRED_SETTINGS:
                    _dot_delete(raw, key)
                    dropped_retired = True
                    continue
                raise SettingsError(f"unknown setting in file: {key}")
        values: dict[str, Any] = {}
        for key, default in defaults.items():
            try:
                values[key] = self._coerce(self._defs[key], _dot_get(raw, key))
            except KeyError:
                values[key] = default
        for key, value in values.items():
            self._validate(self._defs[key], value)
        self._values = values
        if dropped_retired or not self._path.exists():
            self._persist()

    def _apply_env(self, env: dict[str, str]) -> None:
        mapping = {
            "HARNESS_UI_TOKEN": "ui.access_token",
            "HARNESS_BIND_HOST": "ui.bind_host",
            "HARNESS_BIND_PORT": "ui.bind_port",
            "HARNESS_INTERACTIVE_ENDPOINT": "models.interactive.endpoint",
            "HARNESS_BACKGROUND_ENDPOINT": "models.background.endpoint",
            "HARNESS_INTERACTIVE_MODEL": "models.interactive.model_name",
        }
        for env_key, setting_key in mapping.items():
            if env_key in env and env[env_key] != "":
                self._values[setting_key] = self._coerce(self._defs[setting_key], env[env_key])
                self._validate(self._defs[setting_key], self._values[setting_key])

    def _coerce(self, definition: SettingDef, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in {"", "null", "none"}:
            if definition.key.endswith(".seed") or definition.key.endswith("thinking_budget_tokens"):
                return None
        if definition.type == "int":
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise SettingsError(f"{definition.key} must be int") from exc
        if definition.type == "float":
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise SettingsError(f"{definition.key} must be float") from exc
        if definition.type == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in {"1", "true", "yes", "on"}
            return bool(value)
        if definition.type in {"str", "enum"}:
            return value if value is None else str(value)
        return value

    def _validate(self, definition: SettingDef, value: Any) -> None:
        if value is None:
            if definition.key.endswith(".seed") or definition.key.endswith("thinking_budget_tokens"):
                return
            raise SettingsError(f"{definition.key} must not be null")
        if definition.type == "int" and not isinstance(value, int):
            raise SettingsError(f"{definition.key} must be int")
        if definition.type == "float" and not isinstance(value, (int, float)):
            raise SettingsError(f"{definition.key} must be float")
        if definition.type == "bool" and not isinstance(value, bool):
            raise SettingsError(f"{definition.key} must be bool")
        if definition.type in {"str", "enum"} and not isinstance(value, str):
            raise SettingsError(f"{definition.key} must be str")
        if definition.allowed is not None and value not in definition.allowed:
            raise SettingsError(f"{definition.key} must be one of {definition.allowed}")
        if definition.minimum is not None and isinstance(value, (int, float)) and value < definition.minimum:
            raise SettingsError(f"{definition.key} below minimum {definition.minimum}")
        if definition.maximum is not None and isinstance(value, (int, float)) and value > definition.maximum:
            raise SettingsError(f"{definition.key} above maximum {definition.maximum}")

    def _persist(self) -> None:
        tree: dict[str, Any] = {}
        for key, value in self._values.items():
            _dot_set(tree, key, value)
        retries, retry_s = self._retries()
        text = yaml.safe_dump(tree, sort_keys=False, allow_unicode=True)
        atomic_write_text(self._path, text, retries=retries, retry_s=retry_s)

    def get(self, key: str) -> Any:
        if key not in self._defs:
            raise SettingsError(f"unknown setting {key}")
        return deepcopy(self._values[key])

    def set(self, key: str, value: Any, actor: str) -> SettingChange:
        if key not in self._defs:
            raise SettingsError(f"unknown setting {key}")
        coerced = self._coerce(self._defs[key], value)
        self._validate(self._defs[key], coerced)
        old = self._values[key]
        self._values[key] = coerced
        self._persist()
        change = SettingChange(
            id=new_id("chg"),
            ts=now_ts(),
            key=key,
            old_value=old,
            new_value=coerced,
            actor=actor,
        )
        retries, retry_s = self._retries()
        append_jsonl(self._history_path, change.__dict__, retries=retries, retry_s=retry_s)
        if self._events is not None:
            self._events.append(
                Event(
                    id=new_id("evt"),
                    ts=change.ts,
                    actor=actor,
                    operation="settings.set",
                    target=key,
                    result="ok",
                    data={"old": old, "new": coerced},
                )
            )
        return change

    def reset(self, key: str, actor: str) -> SettingChange:
        if key not in self._defs:
            raise SettingsError(f"unknown setting {key}")
        return self.set(key, self._defs[key].default, actor)

    def definitions(self) -> list[SettingDef]:
        return list(self._defs.values())

    def history(self, key: str | None = None) -> list[SettingChange]:
        rows = read_jsonl(self._history_path)
        items = [SettingChange(**row) for row in rows]
        if key is not None:
            items = [item for item in items if item.key == key]
        return items

    def as_dict(self) -> dict[str, Any]:
        return deepcopy(self._values)

    def reload(self) -> None:
        """Re-read the file, falling back to catalog defaults when it is gone.

        A full reset removes the settings file and its change history; reloading
        in place keeps every component's settings reference valid while the
        values return to the seeded defaults (plus launch env overrides).
        """
        self._load()
        self._apply_env(self._env)

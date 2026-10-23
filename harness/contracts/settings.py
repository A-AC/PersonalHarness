from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class SettingsError(ValueError):
    """A settings key or value violated the catalog contract."""


@dataclass
class SettingDef:
    key: str
    type: str
    default: Any
    description: str
    scope: str = "global"
    live: bool = True
    minimum: float | None = None
    maximum: float | None = None
    allowed: tuple[Any, ...] | None = None
    group: str = "general"


@dataclass
class SettingChange:
    id: str
    ts: str
    key: str
    old_value: Any
    new_value: Any
    actor: str


class SettingsService(Protocol):
    def get(self, key: str) -> Any: ...
    def set(self, key: str, value: Any, actor: str) -> SettingChange: ...
    def reset(self, key: str, actor: str) -> SettingChange: ...
    def definitions(self) -> list[SettingDef]: ...
    def history(self, key: str | None = None) -> list[SettingChange]: ...
    def as_dict(self) -> dict[str, Any]: ...

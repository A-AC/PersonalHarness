"""Capability module records: one shape for skills, MCP servers, and web.

These are value objects over the ``modules``, ``module_bindings``, and
``module_tools`` tables. The registry (see ``registry.py``) owns persistence
and eligibility; this module only defines the record shape and validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import util

KINDS = ("skill", "mcp", "web")
STATUSES = ("draft", "active", "disabled", "error")
SCOPES = ("global", "domain", "adaptive")
ORIGINS = ("user", "agent", "system", "import")


def _check(value: str, allowed: tuple[str, ...], field_name: str) -> str:
    if value not in allowed:
        raise ValueError(
            f"invalid module {field_name}: {value!r} "
            f"(expected one of {', '.join(allowed)})"
        )
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


@dataclass(frozen=True)
class Module:
    """A capability module row.

    ``draft`` and ``disabled`` modules are never disclosed or callable; the
    registry enforces that in later phases. ``manifest`` and ``provenance``
    stay structured in Python and JSON in SQLite.
    """

    id: str
    kind: str
    name: str
    version: int = 1
    status: str = "draft"
    scope: str = "global"
    manifest: dict[str, Any] = field(default_factory=dict)
    content_path: str | None = None
    origin: str = "user"
    provenance: dict[str, Any] = field(default_factory=dict)
    created_by: str = "system"
    created_at: str = ""
    updated_at: str = ""
    last_loaded_at: str | None = None
    last_error: str | None = None
    usage_count: int = 0

    def __post_init__(self) -> None:
        if not str(self.id or "").strip():
            raise ValueError("module id must not be empty")
        if not str(self.name or "").strip():
            raise ValueError("module name must not be empty")
        _check(self.kind, KINDS, "kind")
        _check(self.status, STATUSES, "status")
        _check(self.scope, SCOPES, "scope")
        _check(self.origin, ORIGINS, "origin")
        if int(self.version) < 1:
            raise ValueError("module version must be >= 1")
        if not isinstance(self.manifest, dict):
            raise ValueError("module manifest must be a mapping")
        if not isinstance(self.provenance, dict):
            raise ValueError("module provenance must be a mapping")

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "version": int(self.version),
            "status": self.status,
            "scope": self.scope,
            "manifest": util.dumps(self.manifest),
            "content_path": self.content_path,
            "origin": self.origin,
            "provenance": util.dumps(self.provenance),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_loaded_at": self.last_loaded_at,
            "last_error": self.last_error,
            "usage_count": int(self.usage_count),
        }

    @classmethod
    def from_row(cls, row: Any) -> "Module":
        data = _row_dict(row)
        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            name=str(data["name"]),
            version=int(data.get("version") or 1),
            status=str(data.get("status") or "draft"),
            scope=str(data.get("scope") or "global"),
            manifest=util.loads(data.get("manifest") or "", {}),
            content_path=data.get("content_path"),
            origin=str(data.get("origin") or "system"),
            provenance=util.loads(data.get("provenance") or "", {}),
            created_by=str(data.get("created_by") or "system"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            last_loaded_at=data.get("last_loaded_at"),
            last_error=data.get("last_error"),
            usage_count=int(data.get("usage_count") or 0),
        )


@dataclass(frozen=True)
class ModuleBinding:
    """A ``scope=domain`` module's opt-in for one domain."""

    module_id: str
    domain_id: str
    enabled: bool = True
    risk_override: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "module_id": self.module_id,
            "domain_id": self.domain_id,
            "enabled": 1 if self.enabled else 0,
            "risk_override": self.risk_override,
        }

    @classmethod
    def from_row(cls, row: Any) -> "ModuleBinding":
        data = _row_dict(row)
        return cls(
            module_id=str(data["module_id"]),
            domain_id=str(data["domain_id"]),
            enabled=bool(data.get("enabled", 1)),
            risk_override=data.get("risk_override"),
        )


@dataclass(frozen=True)
class ModuleTool:
    """A tool contributed by a module, cached from discovery."""

    module_id: str
    tool_name: str
    schema: dict[str, Any] = field(default_factory=dict)
    risk: str = "confirm"
    side_effects: bool = True
    read_only: bool = False
    last_seen: str = ""

    def __post_init__(self) -> None:
        if not str(self.module_id or "").strip():
            raise ValueError("module tool module_id must not be empty")
        if not str(self.tool_name or "").strip():
            raise ValueError("module tool name must not be empty")
        if not isinstance(self.schema, dict):
            raise ValueError("module tool schema must be a mapping")

    def to_row(self) -> dict[str, Any]:
        return {
            "module_id": self.module_id,
            "tool_name": self.tool_name,
            "schema": util.dumps(self.schema),
            "risk": self.risk,
            "side_effects": 1 if self.side_effects else 0,
            "read_only": 1 if self.read_only else 0,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_row(cls, row: Any) -> "ModuleTool":
        data = _row_dict(row)
        return cls(
            module_id=str(data["module_id"]),
            tool_name=str(data["tool_name"]),
            schema=util.loads(data.get("schema") or "", {}),
            risk=str(data.get("risk") or "confirm"),
            side_effects=bool(data.get("side_effects", 1)),
            read_only=bool(data.get("read_only", 0)),
            last_seen=str(data.get("last_seen") or ""),
        )

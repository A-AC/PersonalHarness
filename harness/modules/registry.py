"""Module registry: persistence and the ``load_all`` seam.

Phase 0 deliberately keeps the existing skill directory and MCP servers file
as the source of truth. ``load_all`` is a no-op placeholder so later phases
can replace those loaders without changing the composition root again.

Until then the registry still owns the module tables and exposes read
helpers, so the UI and tests can inspect records that a later loader writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import util
from ..db import Database
from ..store import Events
from .types import Module, ModuleBinding, ModuleTool


class ModuleRegistry:
    """Read/write access to module records for one data directory."""

    def __init__(self, db: Database, events: Events, config, data_dir: Path):
        self.db = db
        self.events = events
        self.config = config
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / str(config.get("modules.dir", "modules"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.skills_dir = self.root / "skills"

    # -- loading ----------------------------------------------------------

    def load_all(self) -> dict[str, Any]:
        """Discover and register capability modules.

        Phase 0: intentionally empty. Skills and MCP servers are still loaded
        by their existing loaders. The return shape is stable for the callers
        added in Phase 1.
        """
        return {"loaded": [], "skipped": [], "errors": []}

    # -- reads ------------------------------------------------------------

    def all(self) -> list[Module]:
        return [
            Module.from_row(row)
            for row in self.db.query(
                "SELECT * FROM modules ORDER BY name, id"
            )
        ]

    def get(self, module_id: str) -> Module | None:
        row = self.db.query_one(
            "SELECT * FROM modules WHERE id=?", (module_id,)
        )
        return Module.from_row(row) if row else None

    def tools(self, module_id: str | None = None) -> list[ModuleTool]:
        if module_id is None:
            rows = self.db.query(
                "SELECT * FROM module_tools ORDER BY module_id, tool_name"
            )
        else:
            rows = self.db.query(
                "SELECT * FROM module_tools WHERE module_id=?"
                " ORDER BY tool_name", (module_id,)
            )
        return [ModuleTool.from_row(row) for row in rows]

    def bindings(self, module_id: str | None = None) -> list[ModuleBinding]:
        if module_id is None:
            rows = self.db.query(
                "SELECT * FROM module_bindings ORDER BY module_id, domain_id"
            )
        else:
            rows = self.db.query(
                "SELECT * FROM module_bindings WHERE module_id=?"
                " ORDER BY domain_id", (module_id,)
            )
        return [ModuleBinding.from_row(row) for row in rows]

    # -- writes -----------------------------------------------------------

    def save(self, module: Module) -> Module:
        """Insert or replace a module row, keeping its identity."""
        now = util.iso()
        row = module.to_row()
        row["created_at"] = row["created_at"] or now
        row["updated_at"] = now
        self.db.execute(
            "INSERT INTO modules(id, kind, name, version, status, scope,"
            " manifest, content_path, origin, provenance, created_by,"
            " created_at, updated_at, last_loaded_at, last_error, usage_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,"
            " name=excluded.name, version=excluded.version,"
            " status=excluded.status, scope=excluded.scope,"
            " manifest=excluded.manifest, content_path=excluded.content_path,"
            " origin=excluded.origin, provenance=excluded.provenance,"
            " created_by=excluded.created_by, updated_at=excluded.updated_at,"
            " last_loaded_at=excluded.last_loaded_at,"
            " last_error=excluded.last_error, usage_count=excluded.usage_count",
            tuple(row[key] for key in (
                "id", "kind", "name", "version", "status", "scope", "manifest",
                "content_path", "origin", "provenance", "created_by",
                "created_at", "updated_at", "last_loaded_at", "last_error",
                "usage_count",
            )),
        )
        return Module.from_row(row)

    def set_binding(self, binding: ModuleBinding) -> ModuleBinding:
        row = binding.to_row()
        self.db.execute(
            "INSERT INTO module_bindings(module_id, domain_id, enabled,"
            " risk_override) VALUES (?,?,?,?)"
            " ON CONFLICT(module_id, domain_id) DO UPDATE SET"
            " enabled=excluded.enabled, risk_override=excluded.risk_override",
            (row["module_id"], row["domain_id"], row["enabled"],
             row["risk_override"]),
        )
        return binding

    def replace_tools(self, module_id: str,
                      tools: list[ModuleTool]) -> list[ModuleTool]:
        """Replace a module's cached tool discovery in one transaction."""
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM module_tools WHERE module_id=?", (module_id,)
            )
            for tool in tools:
                row = tool.to_row()
                conn.execute(
                    "INSERT INTO module_tools(module_id, tool_name, schema,"
                    " risk, side_effects, read_only, last_seen)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (row["module_id"], row["tool_name"], row["schema"],
                     row["risk"], row["side_effects"], row["read_only"],
                     row["last_seen"]),
                )
        return tools

    def record_error(self, module_id: str, error: str) -> None:
        self.db.execute(
            "UPDATE modules SET status='error', last_error=?, updated_at=?"
            " WHERE id=?", (util.clip(error, 1000), util.iso(), module_id),
        )
        self.events.record(
            "module.unavailable", level="warn",
            data={"module_id": module_id, "error": util.clip(error, 500)},
        )


def load_all(db: Database, events: Events, config, data_dir: Path
             ) -> dict[str, Any]:
    """Convenience wrapper for callers that do not hold a registry object."""
    return ModuleRegistry(db, events, config, data_dir).load_all()

"""Module registry: persistence, eligibility, and the loader seam.

``load_all`` imports the legacy MCP servers file, creates the shipped web
module, discovers/caches tools, registers callable specs, and imports
``SKILL.md`` skills into ``kind=skill`` rows. Skills contribute disclosure
text only; they never register tools.

The registry is also the single place that answers "which modules are active
for this domain?", so the runtime never re-implements binding rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import util
from ..db import Database
from ..store import Events
from .types import Module, ModuleBinding, ModuleTool

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..tools import ToolRegistry


class ModuleRegistry:
    """Read/write access to module records for one data directory."""

    def __init__(
        self, db: Database, events: Events, config, data_dir: Path,
        tools: "ToolRegistry | None" = None,
    ):
        self.db = db
        self.events = events
        self.config = config
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / str(config.get("modules.dir", "modules"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.skills_dir = self.root / "skills"
        # Keep the tool registry off the ``tools()`` read method's name.
        self.tool_registry = tools
        self._loader = None
        self._skills = None

    # -- loading ----------------------------------------------------------

    @property
    def loader(self):
        if self._loader is None:
            from .mcp import McpModuleLoader

            self._loader = McpModuleLoader(
                self, self.tool_registry, self.config, self.events
            )
        return self._loader

    @property
    def skills(self):
        """The skill manager, constructed lazily to avoid a cycle."""
        if self._skills is None:
            from .skills import SkillManager

            self._skills = SkillManager(self)
        return self._skills

    @property
    def status(self) -> dict[str, str]:
        """Per-module human status for diagnostics (``/api/tools``)."""
        return self.loader.status

    def load_all(self) -> dict[str, Any]:
        """Import, discover, and register every eligible capability module."""
        report = self.loader.load_all()
        try:
            imported = self.skills.import_configured()
        except Exception as exc:  # a broken skill never blocks startup
            imported = []
            report.setdefault("errors", []).append({
                "kind": "skill",
                "error": f"{type(exc).__name__}: {exc}",
            })
        if imported:
            report.setdefault("skills_imported", []).extend(imported)
        return report

    def reload(self, module_id: str | None = None) -> dict[str, Any]:
        """Reload one module or all modules without a process restart."""
        if module_id:
            module = self.get(module_id)
            if module is not None and module.kind == "skill":
                return self.skills.reload(module_id)
            return self.loader.reload(module_id)
        report = self.loader.reload(None)
        try:
            imported = self.skills.import_configured()
        except Exception as exc:
            imported = []
            report.setdefault("errors", []).append({
                "kind": "skill",
                "error": f"{type(exc).__name__}: {exc}",
            })
        if imported:
            report.setdefault("skills_imported", []).extend(imported)
        return report

    def set_enabled(self, module_id: str, enabled: bool) -> Module:
        module = self.get(module_id)
        if module is not None and module.kind == "skill":
            return self.skills.set_enabled(module_id, enabled)
        return self.loader.set_enabled(module_id, enabled)

    def health_tick(self, now=None) -> dict[str, Any]:
        return self.loader.health_tick(now)

    def eligible_module_ids(self, domain_id: str | None) -> set[str]:
        """Active modules that may contribute tools in this domain.

        ``global`` is always eligible; ``domain`` requires an enabled binding;
        ``adaptive`` is reserved for skills (Phase 2). Draft, disabled, and
        error modules are never eligible.
        """
        bound = {
            binding.module_id
            for binding in self.bindings()
            if binding.enabled and domain_id is not None
            and binding.domain_id == domain_id
        }
        eligible: set[str] = set()
        for module in self.all():
            if module.status != "active" or module.kind == "skill":
                continue
            if module.scope == "global":
                eligible.add(module.id)
            elif module.scope == "domain" and module.id in bound:
                eligible.add(module.id)
        return eligible

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
        """Mark a module unloadable (invalid manifest/transport)."""
        self.db.execute(
            "UPDATE modules SET status='error', last_error=?, updated_at=?"
            " WHERE id=?", (util.clip(error, 1000), util.iso(), module_id),
        )
        self.events.record(
            "module.unavailable", level="warn",
            data={"module_id": module_id, "error": util.clip(error, 500)},
        )

    def set_last_error(self, module_id: str, error: str) -> None:
        """Record a transient failure without dropping cached capability.

        Health failures keep the module active: cached tools still work and a
        later tick can recover. A turn is never blocked by an unavailable
        server.
        """
        self.db.execute(
            "UPDATE modules SET last_error=?, updated_at=? WHERE id=?",
            (util.clip(error, 1000), util.iso(), module_id),
        )

    def mark_loaded(self, module_id: str) -> None:
        self.db.execute(
            "UPDATE modules SET last_loaded_at=?, last_error=NULL,"
            " updated_at=? WHERE id=?",
            (util.iso(), util.iso(), module_id),
        )


def load_all(db: Database, events: Events, config, data_dir: Path,
             tools: "ToolRegistry | None" = None) -> dict[str, Any]:
    """Convenience wrapper for callers that do not hold a registry object."""
    return ModuleRegistry(db, events, config, data_dir, tools=tools).load_all()

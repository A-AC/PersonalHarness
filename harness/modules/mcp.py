"""Registry-backed MCP module loading.

One loader owns every capability that contributes tools. Modules are rows
(``modules`` + ``module_bindings`` + ``module_tools``); discovery is cached in
``module_tools`` so a turn never launches a process just to build schemas.

The loader is transport-only and deliberately knows nothing about individual
servers: a server's endpoints, credentials, and options belong in that
server's own configuration (passed through ``args``/``env_names``), never in
``config.yaml`` or harness code. Any stdio MCP server listed in
``tools.mcp_servers_file`` is treated exactly like every other one.

Per-call process isolation is preserved: the stdio JSON-RPC client in
``harness/tools.py`` starts a fresh subprocess for every list/call. Health
checks refresh the cache and record ``module.unavailable`` without ever
raising into a turn.

Two registration files are understood, and both may be JSON or YAML:

* ``servers:`` - a list of harness manifests (see ``examples/mcp_servers.yaml``)
* ``mcpServers:`` - the standard mapping shape used by MCP desktop clients

When ``tools.mcp_servers_file`` is empty, ``<data_dir>/mcp.json``,
``mcp_servers.yaml``, and ``mcp_servers.yml`` are picked up automatically.
Explicit reloads re-read the file, so editing a server entry does not require
deleting its module row by hand. Environment *values* in the source file are
written to a private sidecar (``modules/mcp/<name>.env.json``), never to the
module row or SQLite.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import util
from ..tools import (
    RISK_CONFIRM,
    RISK_LOW,
    McpServer,
    ToolDenied,
    ToolError,
    ToolSpec,
    mcp_text,
)
from .types import Module, ModuleBinding, ModuleTool

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Config
    from ..db import Database
    from ..store import Events
    from ..tools import ToolRegistry
    from .registry import ModuleRegistry

# Old system-owned module rows that predate ordinary MCP registration. The
# loader rewrites these once, using the row's own manifest plus any retired
# config group kept by ``Config.removed``. No URL or credential is ever known
# here: the retired group is serialized verbatim into the server's config file.
RETIRED_MODULES = {"web": ""}
MCP_JSON_PARSE_LIMIT = 1_000_000
# Conventional files checked when no explicit import file is configured.
DEFAULT_SERVERS_FILES = ("mcp.json", "mcp_servers.yaml", "mcp_servers.yml")
_DATA_DIR_PLACEHOLDER = re.compile(r"\$\{data_dir\}|\$\{DATA_DIR\}")


def _sanitize(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", str(value or "")).strip("_")


def _sanitize_prefix(value: str) -> str:
    """Keep underscores (a prefix usually ends with one)."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", str(value or ""))



class McpModuleLoader:
    """Discovers, registers, reloads, and health-checks tool modules."""

    def __init__(
        self,
        registry: "ModuleRegistry",
        tools: "ToolRegistry | None",
        config: "Config",
        events: "Events",
    ):
        self.registry = registry
        self.tools = tools
        self.config = config
        self.events = events
        # module id -> human-readable status, surfaced by /api/tools.
        self.status: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Loading / reloading
    # ------------------------------------------------------------------

    def load_all(
        self, *, sync: bool = False, force: bool = False
    ) -> dict[str, Any]:
        report: dict[str, Any] = {"loaded": [], "skipped": [], "errors": []}
        if not bool(self.config.get("modules.enabled", True)):
            return report
        self.migrate_retired_modules()
        self.import_legacy(sync=sync)
        for module in self.registry.all():
            if module.kind != "mcp":
                continue
            if module.status in ("draft", "disabled", "error"):
                report["skipped"].append(module.id)
                continue
            try:
                if self._activate(module, force=force):
                    report["loaded"].append(module.id)
                else:
                    report["errors"].append({
                        "module_id": module.id,
                        "error": self.status.get(module.id, "unavailable"),
                    })
            except Exception as exc:  # a broken module never blocks startup
                message = f"{type(exc).__name__}: {exc}"
                self.registry.set_last_error(module.id, message)
                self.status[module.id] = f"unavailable: {message}"
                report["errors"].append({
                    "module_id": module.id, "error": message,
                })
        return report

    def reload(self, module_id: str | None = None) -> dict[str, Any]:
        """Reload one module, or every module, without restarting the app."""
        if module_id:
            module = self.registry.get(module_id)
            if module is None:
                raise ValueError(f"no such module: {module_id}")
            if self.tools is not None:
                self.tools.unregister_module(module_id)
            if module.status != "active":
                self.events.record(
                    "module.reloaded", data={
                        "module_id": module_id, "status": module.status,
                    },
                )
                return {"module_id": module_id, "status": module.status,
                        "loaded": False}
            loaded = self._activate(module, force=True)
            self.events.record(
                "module.reloaded", level="info" if loaded else "warn",
                data={"module_id": module_id, "loaded": loaded,
                      "status": self.status.get(module_id)},
            )
            return {"module_id": module_id, "loaded": loaded,
                    "status": self.status.get(module_id)}
        if self.tools is not None:
            for module in self.registry.all():
                self.tools.unregister_module(module.id)
        report = self.load_all(sync=True, force=True)
        self.events.record(
            "module.reloaded",
            data={"all": True, "loaded": report["loaded"],
                  "errors": report["errors"]},
        )
        return report

    def set_enabled(self, module_id: str, enabled: bool) -> Module:
        module = self.registry.get(module_id)
        if module is None:
            raise ValueError(f"no such module: {module_id}")
        updated = replace(module, status="active" if enabled else "disabled")
        updated = self.registry.save(updated)
        if self.tools is not None:
            self.tools.unregister_module(module_id)
        if enabled:
            self._activate(updated)
        self.events.record(
            "module.enabled" if enabled else "module.disabled",
            data={"module_id": module_id},
        )
        return self.registry.get(module_id) or updated

    def _activate(self, module: Module, *, force: bool = False) -> bool:
        """Register a module's tools, discovering when needed.

        A lazy module uses its cached discovery. A cache miss (fresh install)
        discovers once so the capability is usable immediately; later starts
        do not launch the server just to build schemas.
        """
        cached = self.registry.tools(module.id)
        startup = str(module.manifest.get("startup") or "")
        lazy = bool(self.config.get("modules.lazy_start", True))
        if force or startup == "eager" or (not lazy and not cached) or not cached:
            if self.discover(module):
                return True
            if not cached:
                return False
        self._register_tools(module, cached)
        return True

    def discover(self, module: Module) -> bool:
        """Run tools/list, replace the cache, and register the tool specs."""
        try:
            server = self._server(module)
            raw_tools = server.list_tools()
            tools = self._module_tools(module, raw_tools)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.registry.set_last_error(module.id, message)
            self.status[module.id] = f"unavailable: {message}"
            self.events.record(
                "module.unavailable", level="warn",
                data={"module_id": module.id, "error": util.clip(message, 500)},
            )
            return False
        self.registry.replace_tools(module.id, tools)
        self.registry.mark_loaded(module.id)
        self.status[module.id] = f"ok ({len(tools)} tools)"
        self.events.record(
            "module.loaded", data={
                "module_id": module.id, "kind": module.kind,
                "tools": [tool.tool_name for tool in tools],
            },
        )
        self._register_tools(module, tools)
        return True

    # ------------------------------------------------------------------
    # Legacy import and retired built-ins
    # ------------------------------------------------------------------

    def import_legacy(self, *, sync: bool = False) -> list[str]:
        """Import MCP server entries that are not yet module rows.

        ``tools.mcp_servers_file`` may be YAML or JSON and may use either the
        harness ``servers:`` list or the standard ``mcpServers:`` mapping. An
        explicit reload (``sync=True``) refreshes existing manifests from the
        file, which is what makes editing or updating a server entry a
        one-command operation.
        """
        path = self._servers_path()
        if path is None:
            return []
        if not path.is_file():
            self.events.record(
                "mcp.config_missing", level="warn", data={"path": str(path)},
            )
            return []
        try:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            self.events.record(
                "mcp.config_error", level="warn",
                data={"error": f"{type(exc).__name__}: {exc}"},
            )
            return []
        imported: list[str] = []
        for entry in self._entries(data):
            name = str(entry.get("name") or "").strip()
            command = str(entry.get("command") or "").strip()
            if not name or not command:
                continue
            module_id = f"mcp:{name}"
            env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
            manifest = self._manifest_from_entry(entry)
            domains = [str(domain) for domain in manifest.get("domains") or []]
            scope = str(entry.get("scope") or "").strip() or (
                "domain" if domains else "global"
            )
            scope = scope if scope in ("global", "domain", "adaptive") else "global"
            now = util.iso()
            existing = self.registry.get(module_id)
            if existing is not None:
                if not sync:
                    continue
                changed = (
                    existing.manifest != manifest
                    or existing.name != name
                    or existing.scope != scope
                )
                status = existing.status
                # A user-disabled module stays disabled. Anything else is
                # reset so a fixed entry can recover from an old error.
                if status == "error":
                    status = "active"
                if changed or status != existing.status:
                    updated = replace(
                        existing,
                        name=name,
                        manifest=manifest,
                        scope=scope,
                        status=status,
                        provenance={
                            **dict(existing.provenance or {}),
                            "source": str(path),
                        },
                        updated_at=now,
                    )
                    self.registry.save(updated)
                if changed:
                    # Cached discovery may reflect the old tools_allow / risk
                    # settings; drop it so the next activation re-discovers.
                    self.registry.replace_tools(module_id, [])
                self._store_env_values(name, env)
                for domain_id in domains:
                    self.registry.set_binding(ModuleBinding(
                        module_id=module_id, domain_id=domain_id, enabled=True,
                    ))
                imported.append(module_id)
                if changed or status != existing.status:
                    self.events.record(
                        "module.updated", data={
                            "module_id": module_id, "source": str(path),
                        },
                    )
                continue
            module = Module(
                id=module_id,
                kind="mcp",
                name=name,
                version=1,
                status="active",
                scope=scope,
                manifest=manifest,
                origin="import",
                provenance={"source": str(path)},
                created_by="import",
                created_at=now,
                updated_at=now,
            )
            self.registry.save(module)
            self._store_env_values(name, env)
            for domain_id in domains:
                self.registry.set_binding(ModuleBinding(
                    module_id=module_id, domain_id=domain_id, enabled=True,
                ))
            imported.append(module_id)
            self.events.record(
                "module.imported", data={
                    "module_id": module_id, "source": str(path),
                },
            )
        return imported

    def _servers_path(self) -> Path | None:
        """Resolve the configured import file, or a conventional default."""
        path_text = str(self.config.get("tools.mcp_servers_file", "") or "")
        path_text = path_text.strip()
        if path_text:
            path = Path(path_text).expanduser()
            if not path.is_absolute():
                path = self.registry.data_dir / path
            return path
        for name in DEFAULT_SERVERS_FILES:
            candidate = self.registry.data_dir / name
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _entries(data: Any) -> list[dict[str, Any]]:
        """Accept harness ``servers:`` lists and standard ``mcpServers:``."""
        entries: list[dict[str, Any]] = []
        if isinstance(data, list):
            entries.extend(entry for entry in data if isinstance(entry, dict))
        if isinstance(data, dict):
            servers = data.get("servers")
            if isinstance(servers, list):
                entries.extend(
                    entry for entry in servers if isinstance(entry, dict)
                )
            elif isinstance(servers, dict):
                entries.extend(
                    {**entry, "name": str(name)}
                    for name, entry in servers.items()
                    if isinstance(entry, dict)
                )
            standard = data.get("mcpServers")
            if isinstance(standard, dict):
                entries.extend(
                    {**entry, "name": str(name)}
                    for name, entry in standard.items()
                    if isinstance(entry, dict)
                )
        return entries

    def _store_env_values(self, name: str, env: dict[str, Any]) -> None:
        """Keep explicit env values in a private sidecar, never in SQLite."""
        directory = self.registry.root / "mcp"
        path = directory / f"{_sanitize(name)}.env.json"
        values = {
            str(key): str(value)
            for key, value in env.items()
            if str(value or "").strip() != ""
        }
        if not values:
            # The source now relies on the process environment; drop any
            # values left over from an earlier version of the file.
            try:
                path.unlink()
            except OSError:
                pass
            return
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(util.dumps(values) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self.events.record(
            "mcp.env_values_stored", data={
                "module_id": f"mcp:{name}",
                "path": str(path),
                "env_names": sorted(values),
            },
        )

    def migrate_retired_modules(self) -> list[str]:
        """Rewrite system-owned legacy rows as ordinary MCP modules.

        The old row already carries its transport; the only extra input is the
        retired config group from ``config.yaml``. It is serialized verbatim
        into ``modules/mcp/<name>.json`` and passed with ``--config``, so the
        harness stores no endpoint or credential knowledge of its own.
        """
        migrated: list[str] = []
        removed = dict(getattr(self.config, "removed", {}) or {})
        for legacy_id, default_prefix in RETIRED_MODULES.items():
            module = self.registry.get(legacy_id)
            if module is None or module.kind != "mcp":
                continue
            target_id = f"mcp:{legacy_id}"
            if self.registry.get(target_id) is not None:
                # Already re-registered by the user; drop the stale row.
                self._delete_module_rows(legacy_id)
                migrated.append(target_id)
                continue
            manifest = dict(module.manifest or {})
            manifest.setdefault("tool_prefix", default_prefix)
            # Keep the legacy variable names: they are generic transport
            # metadata, contain no values, and let an existing environment
            # override survive the migration (the server owns precedence).
            manifest["env_names"] = [
                str(name) for name in manifest.get("env_names") or []
                if str(name).strip()
            ]
            config_path = ""
            group = removed.get(legacy_id)
            if isinstance(group, dict) and group:
                config_path = self._write_mcp_config(legacy_id, group)
                manifest["args"] = self._with_config_arg(
                    [str(arg) for arg in manifest.get("args") or []],
                    config_path,
                )
            updated = replace(
                module,
                id=target_id,
                manifest=manifest,
                provenance={
                    **dict(module.provenance or {}),
                    "migrated_from": legacy_id,
                },
                updated_at=util.iso(),
            )
            # Reparent cached discovery and bindings before replacing the row.
            self.registry.db.execute(
                "UPDATE module_tools SET module_id=? WHERE module_id=?",
                (target_id, legacy_id),
            )
            self.registry.db.execute(
                "UPDATE module_bindings SET module_id=? WHERE module_id=?",
                (target_id, legacy_id),
            )
            self.registry.save(updated)
            self.registry.db.execute(
                "DELETE FROM modules WHERE id=?", (legacy_id,),
            )
            migrated.append(target_id)
            self.events.record(
                "module.migrated", data={
                    "from": legacy_id,
                    "to": target_id,
                    "config_file": config_path,
                },
            )
        return migrated

    def _delete_module_rows(self, module_id: str) -> None:
        self.registry.db.execute(
            "DELETE FROM module_tools WHERE module_id=?", (module_id,),
        )
        self.registry.db.execute(
            "DELETE FROM module_bindings WHERE module_id=?", (module_id,),
        )
        self.registry.db.execute(
            "DELETE FROM modules WHERE id=?", (module_id,),
        )

    def _write_mcp_config(self, name: str, values: dict[str, Any]) -> str:
        directory = self.registry.root / "mcp"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_sanitize(name)}.json"
        if not path.exists():
            # Verbatim: this loader never interprets individual settings.
            path.write_text(
                util.dumps(values) + "\n", encoding="utf-8",
            )
        return str(path)

    @staticmethod
    def _with_config_arg(args: list[str], config_path: str) -> list[str]:
        cleaned: list[str] = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg == "--config":
                index += 2
                continue
            if arg.startswith("--config="):
                index += 1
                continue
            cleaned.append(arg)
            index += 1
        return [*cleaned, "--config", config_path]

    @staticmethod
    def _manifest_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
        autoapprove = bool(entry.get("autoapprove", False))

        def _strings(value: Any) -> list[str]:
            if isinstance(value, (list, tuple)):
                return [str(item) for item in value]
            if isinstance(value, str) and value.strip():
                return [value.strip()]
            return []

        try:
            timeout = float(entry.get("timeout_s") or 30.0)
        except (TypeError, ValueError):
            timeout = 30.0
        manifest: dict[str, Any] = {
            "transport": str(
                entry.get("transport") or entry.get("type") or "stdio"
            ),
            "command": str(entry.get("command") or ""),
            "args": _strings(entry.get("args")),
            "env_names": sorted((entry.get("env") or {}).keys())
            if isinstance(entry.get("env"), dict) else [],
            "domains": _strings(entry.get("domains")),
            "autoapprove": autoapprove,
            "tools_allow": _strings(entry.get("tools_allow")),
            "tools_deny": _strings(entry.get("tools_deny")),
            "read_only": bool(entry.get("read_only", False)),
            "startup": str(entry.get("startup") or "lazy"),
            "timeout_s": max(0.1, timeout),
        }
        if entry.get("risk_default"):
            manifest["risk_default"] = str(entry["risk_default"])
        if "side_effects" in entry:
            manifest["side_effects"] = bool(entry["side_effects"])
        if "tool_prefix" in entry:
            # Empty string is meaningful: register the server's own tool names
            # verbatim instead of the default ``mcp_<server>_`` prefix.
            manifest["tool_prefix"] = str(entry["tool_prefix"] or "")
        return manifest

    # ------------------------------------------------------------------
    # Discovery -> cached records -> ToolSpecs
    # ------------------------------------------------------------------

    def _server(self, module: Module) -> McpServer:
        manifest = module.manifest or {}
        transport = str(manifest.get("transport") or "stdio")
        if transport != "stdio":
            raise ValueError(
                f"unsupported module transport: {transport} (only stdio)"
            )
        command = self._expand(str(manifest.get("command") or "").strip())
        if not command:
            raise ValueError("module manifest has no command")
        args = [self._expand(str(arg)) for arg in manifest.get("args") or []]
        return McpServer(
            module.id, command, args, self._env(module),
            timeout_s=float(manifest.get("timeout_s") or 30.0),
        )

    def _expand(self, value: str) -> str:
        """Expand ``${data_dir}`` so one manifest works locally and in Docker."""
        if "${" not in value:
            return value
        data_dir = str(self.registry.data_dir.resolve())
        return _DATA_DIR_PLACEHOLDER.sub(lambda _match: data_dir, value)

    def _env(self, module: Module) -> dict[str, str]:
        """Forward only the variables the server declared.

        Process environment values are used when present. Explicit values from
        a standard ``mcpServers`` file live in a private sidecar written at
        import time; they override the process environment. They are never
        copied into the module row or SQLite.
        """
        names = [str(name) for name in module.manifest.get("env_names") or []]
        env = {name: os.environ[name] for name in names if name in os.environ}
        sidecar = self._env_path(module)
        if sidecar is not None and sidecar.is_file():
            values = util.loads(sidecar.read_text(encoding="utf-8"), {})
            if isinstance(values, dict):
                for name in names:
                    value = values.get(name)
                    if value is not None and str(value).strip() != "":
                        env[name] = str(value)
        return env

    def _env_path(self, module: Module) -> Path | None:
        name = module.id.split(":", 1)[-1]
        if not name:
            return None
        return self.registry.root / "mcp" / f"{_sanitize(name)}.env.json"

    def _module_tools(
        self, module: Module, raw_tools: list[dict[str, Any]]
    ) -> list[ModuleTool]:
        manifest = module.manifest or {}
        allow = {str(name) for name in manifest.get("tools_allow") or []}
        deny = {str(name) for name in manifest.get("tools_deny") or []}
        autoapprove = bool(manifest.get("autoapprove", False))
        risk = str(
            manifest.get("risk_default")
            or (RISK_LOW if autoapprove else self.config.get(
                "modules.default_mcp_risk", RISK_CONFIRM
            ))
        )
        if risk not in (RISK_LOW, RISK_CONFIRM):
            risk = RISK_CONFIRM
        read_only = bool(manifest.get("read_only", False))
        side_effects = bool(
            manifest["side_effects"] if "side_effects" in manifest
            else not read_only
        )
        now = util.iso()
        tools: list[ModuleTool] = []
        seen: set[str] = set()
        for raw in raw_tools:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name or name in seen:
                continue
            if allow and name not in allow:
                continue
            if name in deny:
                continue
            seen.add(name)
            schema = dict(
                raw.get("inputSchema")
                or {"type": "object", "properties": {}}
            )
            # module_tools has no description column; keep it beside the
            # schema under a private key and strip it when registering.
            schema["_harness_description"] = str(
                raw.get("description") or ""
            )
            tools.append(ModuleTool(
                module_id=module.id,
                tool_name=name,
                schema=schema,
                risk=risk,
                side_effects=side_effects,
                read_only=read_only,
                last_seen=now,
            ))
        return tools

    def _registered_name(self, module: Module, tool_name: str) -> str:
        manifest = module.manifest or {}
        if "tool_prefix" in manifest:
            prefix = _sanitize_prefix(str(manifest.get("tool_prefix") or ""))
        else:
            base = module.id.split(":", 1)[-1]
            prefix = f"mcp_{_sanitize(base)}_"
        return f"{prefix}{_sanitize(tool_name)}"

    def _register_tools(
        self, module: Module, tools: list[ModuleTool]
    ) -> None:
        if self.tools is None:
            return
        for tool in tools:
            name = self._registered_name(module, tool.tool_name)
            existing = self.tools.resolve_optional(name)
            if existing is not None and existing.module != module.id:
                self.events.record(
                    "module.tool_refused", level="warn",
                    data={"module_id": module.id, "tool": name,
                          "error": "name already registered"},
                )
                continue
            description = str(
                tool.schema.get("_harness_description") or tool.tool_name
            )
            parameters = {
                key: value for key, value in tool.schema.items()
                if not key.startswith("_harness_")
            }
            try:
                self.tools.register(ToolSpec(
                    name=name,
                    description=f"[{module.name}] {description}",
                    parameters=parameters,
                    risk=tool.risk,
                    handler=self._make_handler(module, tool.tool_name),
                    module=module.id,
                    side_effects=tool.side_effects,
                    read_only=tool.read_only,
                    untrusted_output=True,
                    scope=module.scope,
                ))
            except (ToolDenied, ToolError) as exc:
                self.events.record(
                    "module.tool_refused", level="warn",
                    data={"module_id": module.id, "tool": name,
                          "error": str(exc)},
                )

    def _make_handler(self, module: Module, tool_name: str):
        server = self._server(module)

        def handler(ctx, args: dict[str, Any]) -> dict[str, Any]:
            result = server.call(tool_name, args)
            # A single JSON object is exposed as structured data (useful for
            # taint provenance such as a source URL). Everything else stays
            # plain MCP text, clipped once with the normal observation limit.
            text = mcp_text(result, limit=None)
            if text and len(text) <= MCP_JSON_PARSE_LIMIT:
                parsed = util.loads(text, None)
                if isinstance(parsed, dict):
                    return parsed
            return {"content": mcp_text(result)}

        return handler

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health_tick(self, now=None) -> dict[str, Any]:
        """Refresh discovery for active modules, at most every interval."""
        report: dict[str, Any] = {
            "checked": 0, "errors": [], "module_ids": [], "skipped": True,
        }
        if not bool(self.config.get("modules.enabled", True)):
            return report
        interval = float(self.config.get("modules.health_interval_s", 300.0))
        now = now or util.now_utc()
        last = self.registry.db.meta_get("modules.last_health_at")
        if last and (now - util.parse_iso(last)).total_seconds() < interval:
            return report
        report["skipped"] = False
        for module in self.registry.all():
            if module.kind != "mcp" or module.status != "active":
                continue
            report["module_ids"].append(module.id)
            report["checked"] += 1
            if not self.discover(module):
                report["errors"].append(module.id)
        self.registry.db.meta_set("modules.last_health_at", util.iso(now))
        return report

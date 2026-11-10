"""Registry-backed MCP and web module loading.

One loader owns every capability that contributes tools: MCP servers imported
from the legacy YAML file and the shipped web-search module. Modules are rows
(``modules`` + ``module_bindings`` + ``module_tools``); discovery is cached in
``module_tools`` so a turn never launches a process just to build schemas.

Per-call process isolation is preserved: the stdio JSON-RPC client in
``harness/tools.py`` starts a fresh subprocess for every list/call. Health
checks refresh the cache and record ``module.unavailable`` without ever
raising into a turn.
"""

from __future__ import annotations

import os
import re
import sys
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

WEB_MODULE_ID = "web"
WEB_SERVER = Path(__file__).resolve().parent / "web" / "server.py"

# Config values the web MCP server reads from its environment. The manifest
# stores these names, never the values (see invariant 11).
WEB_ENV_NAMES = (
    "HARNESS_WEB_SEARXNG_URL",
    "HARNESS_WEB_MAX_RESULTS",
    "HARNESS_WEB_TIMEOUT_S",
    "HARNESS_WEB_CATEGORIES",
    "HARNESS_WEB_SAFE_SEARCH",
)


def _sanitize(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", str(value or "")).strip("_")


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

    def load_all(self) -> dict[str, Any]:
        report: dict[str, Any] = {"loaded": [], "skipped": [], "errors": []}
        if not bool(self.config.get("modules.enabled", True)):
            return report
        self.import_legacy()
        self.ensure_web_module()
        for module in self.registry.all():
            if module.kind not in ("mcp", "web"):
                continue
            if module.status in ("draft", "disabled", "error"):
                report["skipped"].append(module.id)
                continue
            try:
                if self._activate(module):
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
        report = self.load_all()
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
    # Legacy import and the shipped web module
    # ------------------------------------------------------------------

    def import_legacy(self) -> list[str]:
        """Import ``tools.mcp_servers_file`` entries that are not yet rows.

        The file stays supported as an import source: existing module rows are
        never overwritten, so edits made through the API/UI survive.
        """
        path_text = str(self.config.get("tools.mcp_servers_file", "") or "")
        if not path_text:
            return []
        path = Path(path_text)
        if not path.is_absolute():
            path = self.registry.data_dir / path
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
        for entry in data.get("servers") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            command = str(entry.get("command") or "").strip()
            if not name or not command:
                continue
            module_id = f"mcp:{name}"
            if self.registry.get(module_id) is not None:
                continue
            legacy_env = entry.get("env") or {}
            if isinstance(legacy_env, dict) and any(
                str(value or "").strip() for value in legacy_env.values()
            ):
                # Invariant: editable state stores variable names, not values.
                self.events.record(
                    "mcp.env_values_ignored", level="warn",
                    data={
                        "module_id": module_id,
                        "env_names": sorted(legacy_env),
                        "note": "set these variables in the harness "
                                "environment; values are not stored",
                    },
                )
            manifest = self._manifest_from_entry(entry)
            domains = [
                str(domain).strip() for domain in entry.get("domains") or []
                if str(domain).strip()
            ]
            scope = str(entry.get("scope") or "").strip() or (
                "domain" if domains else "global"
            )
            now = util.iso()
            module = Module(
                id=module_id,
                kind="mcp",
                name=name,
                version=1,
                status="active",
                scope=scope if scope in ("global", "domain", "adaptive")
                else "global",
                manifest=manifest,
                origin="import",
                provenance={"source": str(path)},
                created_by="import",
                created_at=now,
                updated_at=now,
            )
            self.registry.save(module)
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

    def ensure_web_module(self) -> Module:
        """Create/refresh the system-owned web-search module row."""
        enabled = bool(self.config.get("web.enabled", True))
        manifest = self._web_manifest()
        existing = self.registry.get(WEB_MODULE_ID)
        if existing is None:
            now = util.iso()
            module = Module(
                id=WEB_MODULE_ID,
                kind="web",
                name="Web search (SearXNG)",
                version=1,
                status="active" if enabled else "disabled",
                scope="global",
                manifest=manifest,
                origin="system",
                provenance={"source": "builtin:web"},
                created_by="system",
                created_at=now,
                updated_at=now,
            )
            self.registry.save(module)
            return module
        if existing.origin == "system":
            # Refresh transport fields (server path, env names, timeout) while
            # preserving the user's enable/disable decision. Setting
            # web.enabled=false disables it; enabling again is an explicit
            # API/CLI action.
            status = existing.status
            if not enabled:
                status = "disabled"
            refreshed = replace(
                existing,
                name="Web search (SearXNG)",
                manifest={**existing.manifest, **manifest},
                status=status,
            )
            return self.registry.save(refreshed)
        return existing

    def _web_manifest(self) -> dict[str, Any]:
        timeout = max(1.0, float(self.config.get("web.timeout_s", 10.0) or 10.0))
        return {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(WEB_SERVER)],
            "env_names": list(WEB_ENV_NAMES),
            "domains": [],
            "autoapprove": True,
            "risk_default": RISK_LOW,
            "tools_allow": ["web_search"],
            "tools_deny": [],
            "read_only": True,
            "side_effects": False,
            "startup": "lazy",
            # The MCP request must outlive the HTTP request inside the server.
            "timeout_s": timeout + 5.0,
        }

    @staticmethod
    def _manifest_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
        autoapprove = bool(entry.get("autoapprove", False))
        manifest: dict[str, Any] = {
            "transport": str(entry.get("transport") or "stdio"),
            "command": str(entry.get("command") or ""),
            "args": [str(arg) for arg in entry.get("args") or []],
            "env_names": sorted((entry.get("env") or {}).keys())
            if isinstance(entry.get("env"), dict) else [],
            "domains": [str(d) for d in entry.get("domains") or []],
            "autoapprove": autoapprove,
            "tools_allow": [str(t) for t in entry.get("tools_allow") or []],
            "tools_deny": [str(t) for t in entry.get("tools_deny") or []],
            "read_only": bool(entry.get("read_only", False)),
            "startup": str(entry.get("startup") or "lazy"),
            "timeout_s": float(entry.get("timeout_s") or 30.0),
        }
        if entry.get("risk_default"):
            manifest["risk_default"] = str(entry["risk_default"])
        if "side_effects" in entry:
            manifest["side_effects"] = bool(entry["side_effects"])
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
        command = str(manifest.get("command") or "").strip()
        if not command:
            raise ValueError("module manifest has no command")
        args = [str(arg) for arg in manifest.get("args") or []]
        return McpServer(
            module.id, command, args, self._env(module),
            timeout_s=float(manifest.get("timeout_s") or 30.0),
        )

    def _env(self, module: Module) -> dict[str, str]:
        names = [str(name) for name in module.manifest.get("env_names") or []]
        env = {name: os.environ[name] for name in names if name in os.environ}
        if module.kind == "web":
            # Values come from config at launch time; the manifest stores only
            # the variable names, so secrets/config stay out of module rows.
            env.update({
                "HARNESS_WEB_SEARXNG_URL": str(
                    self.config.get("web.searxng_url", "") or ""
                ),
                "HARNESS_WEB_MAX_RESULTS": str(
                    self.config.get("web.max_results", 5)
                ),
                "HARNESS_WEB_TIMEOUT_S": str(
                    self.config.get("web.timeout_s", 10.0)
                ),
                "HARNESS_WEB_CATEGORIES": str(
                    self.config.get("web.categories", "general") or ""
                ),
                "HARNESS_WEB_SAFE_SEARCH": str(
                    self.config.get("web.safe_search", 1)
                ),
            })
        return env

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
        if module.kind == "web":
            return _sanitize(tool_name)
        base = module.id.split(":", 1)[-1]
        return f"mcp_{_sanitize(base)}_{_sanitize(tool_name)}"

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
            if module.kind == "web":
                # Parse the module's structured JSON without the generic
                # 4000-character observation clip; the tool result is clipped
                # once by the executor. A malformed payload is a clean error.
                text = "\n".join(
                    str(item.get("text", ""))
                    for item in result.get("content") or []
                    if isinstance(item, dict) and item.get("type") == "text"
                )
                parsed = util.loads(text, None)
                if not isinstance(parsed, dict):
                    raise ValueError(
                        "web module returned a malformed JSON response"
                    )
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
            if module.kind not in ("mcp", "web") or module.status != "active":
                continue
            report["module_ids"].append(module.id)
            report["checked"] += 1
            if not self.discover(module):
                report["errors"].append(module.id)
        self.registry.db.meta_set("modules.last_health_at", util.iso(now))
        return report

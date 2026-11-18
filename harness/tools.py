"""Tool execution: discovery, validation, permissions, dispatch, records.

The model proposes tool calls; the harness resolves the tool, validates
arguments against the schema, applies the permission policy, requests
approval when required, executes, records the outcome, and returns a bounded
observation. There is no shell tool, no arbitrary command execution, and no
generic code-execution tool — this is enforced by refusing to register any
tool whose name matches the banned patterns, regardless of configuration
source. MCP results and skills are untrusted data and can never widen
permissions.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import util
from .config import save_config
from .db import Database, rows_to_dicts
from .memory import MemoryError, MemoryManager
from .model import ToolCall
from .store import Domains, Events, Messages, Tasks

RISK_LOW = "low"
RISK_CONFIRM = "confirm"

# Fields persisted with a run's capability snapshot. The prompt schema and
# every permission decision made for that run read these frozen values, so a
# module reload or registry replacement cannot change an existing run.
FROZEN_SPEC_FIELDS = (
    "name", "description", "parameters", "risk", "module", "side_effects",
    "external_egress", "read_only", "untrusted_output", "scope",
    "summary_template",
)

# Only these inherited variables reach an MCP subprocess. They are OS/runtime
# essentials: locating an absolute interpreter, temp files, locale, and the
# Windows system directories. Harness/model secrets and every unrelated
# variable stay in the parent process. Configured servers add explicit values
# through their manifest ``env`` (stored in the private sidecar), and only the
# names they declare are inherited from the harness environment.
MCP_ENV_ALLOWLIST = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "USER", "USERNAME", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "LC_MESSAGES", "TZ", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
})


def mcp_process_env(
    declared: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build one MCP child's environment from an allowlist + declared values.

    Nothing else from ``os.environ`` is forwarded. Process lifetime is not a
    security boundary: a configured server is native code and can read its own
    files and the network, which is why the module surface is small and the
    command allowlist exists. This function only prevents accidental secret
    inheritance.
    """
    env = {
        name: os.environ[name]
        for name in MCP_ENV_ALLOWLIST
        if name in os.environ
    }
    for name, value in (declared or {}).items():
        if value is None:
            continue
        text = str(name)
        if not text:
            continue
        env[text] = str(value)
    return env

# Sub-agents may only use low-risk tools and may never delegate again or create
# domains. Progress is reported to the parent as a normal tool result.
SUBAGENT_BLOCKED_NAMES = frozenset({"delegate_task", "create_domain"})

# No tool with one of these name patterns can ever be registered.
_BANNED = re.compile(
    r"^(bash|sh|shell|cmd|command|exec|execute|run|powershell|pwsh|python|"
    r"python3|eval|system|terminal|console|os_system|subprocess|code_exec)",
    re.IGNORECASE,
)


class ToolError(ValueError):
    """Invalid tool call (unknown tool, bad arguments, banned name)."""


class ToolDenied(PermissionError):
    """The permission policy denies this tool outright."""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: str
    handler: Callable[["ToolContext", dict[str, Any]], dict[str, Any]]
    # Capability attribution. Builtins belong to the harness itself; module
    # tools carry the owning module id (filled from the registry).
    module: str = "builtin"
    # Trust metadata. ``side_effects`` means the tool can change the world
    # outside the harness; ``read_only`` is an explicit module declaration;
    # ``untrusted_output`` means the result text is not authored by the user
    # and taints the run for later steps. Unknown provenance is treated as
    # side-effecting: only builtins and modules that opt in are exempt.
    side_effects: bool = False
    # Can transmit prompt-derived arguments to an external system. This is
    # independent of mutation: a read-only web request can still exfiltrate.
    external_egress: bool = False
    read_only: bool = False
    untrusted_output: bool = False
    # Eligibility scope of the owning module (global | domain | adaptive).
    scope: str = "global"
    # Optional human-readable summary template for approval cards.
    summary_template: str = ""


@dataclass
class ToolContext:
    """Everything a tool handler may touch. Assembled by the runtime."""
    run_id: str
    task_id: str
    domain_id: str | None = None
    memory: MemoryManager | None = None
    domains: Domains | None = None
    tasks: Tasks | None = None
    messages: Messages | None = None
    skills: Any = None
    subagents: Any = None
    scheduler: Any = None
    role: str = "user"
    # First untrusted origin this run consumed ('' when the run is clean).
    # Memory handlers use it to mark provenance and revision reasons.
    tainted_origin: str = ""
    # Optional runtime callback used by ``use_domain`` to disclose the
    # entered domain's environment in the tool result.
    briefing: Callable[[str], dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_arguments(
    spec: ToolSpec, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Validate against the JSON-schema-lite spec. Reports all problems."""
    problems: list[str] = []
    schema = spec.parameters or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    if not isinstance(arguments, dict):
        raise ToolError(f"{spec.name}: arguments must be a JSON object")
    cleaned: dict[str, Any] = {}
    for key, value in arguments.items():
        if key not in props:
            problems.append(f"unknown argument: {key}")
            continue
        expected = props[key].get("type")
        if expected and not isinstance(value, _TYPES.get(expected, object)):
            problems.append(
                f"argument {key} must be {expected}, got {type(value).__name__}"
            )
            continue
        if expected == "string" and not str(value).strip():
            problems.append(f"argument {key} must not be empty")
            continue
        cleaned[key] = value
    for key in required:
        if key not in cleaned:
            problems.append(f"missing required argument: {key}")
        elif str(cleaned.get(key)).strip() in ("", "<...>", "placeholder"):
            problems.append(f"argument {key} still contains a placeholder")
    if problems:
        raise ToolError(
            f"{spec.name}: invalid arguments: " + "; ".join(problems)
        )
    return cleaned


def validate_create_domain_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Require useful routing evidence in a model-proposed domain."""
    for field in ("keywords", "examples"):
        values = arguments.get(field)
        if not isinstance(values, list) or not any(
            isinstance(item, str) and item.strip() for item in values
        ):
            raise ToolError(
                f"create_domain: argument {field} must contain at least one "
                "non-empty value"
            )
    return arguments


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    def __init__(
        self,
        db: Database,
        events: Events,
        config,
        artifacts_dir: Path,
    ):
        self.db = db
        self.events = events
        self.config = config
        self.artifacts_dir = artifacts_dir
        self._tools: dict[str, ToolSpec] = {}
        # Declared risk before any persistent per-tool override.
        self._base_risks: dict[str, str] = {}
        # Guards every read and mutation of the tool map. It is deliberately
        # never held while a handler runs or while an MCP subprocess starts:
        # callers resolve a spec under the lock and then act on the copy.
        self._lock = threading.RLock()
        self._register_builtins()

    # -- registration -----------------------------------------------------

    def _prepare(self, spec: ToolSpec) -> ToolSpec:
        """Validate one spec and apply persistent risk overrides."""
        if _BANNED.match(spec.name):
            raise ToolDenied(
                f"refusing to register tool '{spec.name}': shell/command "
                "execution tools are not permitted"
            )
        if spec.risk not in (RISK_LOW, RISK_CONFIRM):
            raise ToolError(f"invalid risk class for {spec.name}: {spec.risk}")
        overrides = self.config.get("tools.risk_overrides", {}) or {}
        effective = overrides.get(spec.name, spec.risk)
        # Domain instructions are user-authored state.  Unlike ordinary
        # side-effecting tools, this write always remains approval-gated.
        if spec.name == "edit_domain":
            effective = RISK_CONFIRM
        if effective not in (RISK_LOW, RISK_CONFIRM):
            raise ToolError(f"invalid risk override for {spec.name}")
        return ToolSpec(
            name=spec.name, description=spec.description,
            parameters=spec.parameters, risk=effective, handler=spec.handler,
            module=spec.module, side_effects=bool(spec.side_effects),
            external_egress=bool(spec.external_egress),
            read_only=bool(spec.read_only),
            untrusted_output=bool(spec.untrusted_output),
            scope=spec.scope,
            summary_template=self._sanitize_summary_template(
                spec.summary_template
            ),
        )

    def register(self, spec: ToolSpec) -> None:
        prepared = self._prepare(spec)
        with self._lock:
            self._base_risks[prepared.name] = spec.risk
            self._tools[prepared.name] = prepared

    def replace_module_tools(
        self, module_id: str, specs: list[ToolSpec]
    ) -> list[tuple[str, str]]:
        """Atomically replace every tool owned by one module.

        Discovery (including the heartbeat) uses this so a tool the server no
        longer advertises disappears instead of lingering forever. The swap
        happens under the registry lock, so a concurrent iteration sees the
        whole old set or the whole new set, never a partial mutation.

        Returns the refused ``(name, reason)`` pairs so the caller can record
        them; a name owned by another module is never taken over. Every
        prepared name is checked against other owners before anything is
        removed, so a collision leaves the module's previous surface untouched
        instead of dropping tools the replacement cannot install.
        """
        prepared: list[tuple[ToolSpec, str]] = []
        refused: list[tuple[str, str]] = []
        for spec in specs:
            try:
                prepared.append((self._prepare(spec), spec.risk))
            except (ToolDenied, ToolError) as exc:
                refused.append((spec.name, str(exc)))
        with self._lock:
            collisions = [
                (spec.name, "name already registered")
                for spec, _ in prepared
                if (existing := self._tools.get(spec.name)) is not None
                and existing.module != module_id
            ]
            if collisions:
                # Refuse the whole replacement: keeping the old surface is
                # strictly better than deleting it for a swap that cannot be
                # applied in full.
                return refused + collisions
            for name in [
                name for name, spec in self._tools.items()
                if spec.module == module_id
            ]:
                self._tools.pop(name, None)
                self._base_risks.pop(name, None)
            for spec, base_risk in prepared:
                self._tools[spec.name] = spec
                self._base_risks[spec.name] = base_risk
        return refused

    @staticmethod
    def _sanitize_summary_template(value: Any) -> str:
        """Keep one bounded line for an approval summary template."""
        if not isinstance(value, str):
            return ""
        return util.clip(" ".join(value.split()), 200)

    def risk_override(self, name: str) -> str | None:
        """Return the persisted override for a known tool, if any."""
        spec = self.resolve_optional(name)
        if spec is None:
            raise ToolError(f"unknown tool: {name}")
        overrides = self.config.get("tools.risk_overrides", {}) or {}
        if not isinstance(overrides, dict):
            return None
        value = overrides.get(name)
        return value if value in (RISK_LOW, RISK_CONFIRM) else None

    def set_risk_override(self, name: str, risk: str | None) -> None:
        """Set or clear a persistent per-tool risk override."""
        with self._lock:
            spec = self._tools.get(name)
            if spec is None:
                raise ToolError(f"unknown tool: {name}")
            if risk not in (RISK_LOW, RISK_CONFIRM, None):
                raise ToolError(
                    f"invalid risk override for {name}: {risk}; expected "
                    "'low', 'confirm', or null"
                )
            if risk == RISK_LOW and spec.side_effects:
                raise ToolError(
                    f"cannot lower risk for side-effecting tool {name} to 'low'"
                )
            effective = risk or self._base_risks.get(name, spec.risk)
            if effective not in (RISK_LOW, RISK_CONFIRM):
                raise ToolError(
                    f"invalid effective risk for {name}: {effective}"
                )
            self._tools[name] = ToolSpec(
                name=spec.name, description=spec.description,
                parameters=spec.parameters, risk=effective,
                handler=spec.handler, module=spec.module,
                side_effects=bool(spec.side_effects),
                external_egress=bool(spec.external_egress),
                read_only=bool(spec.read_only),
                untrusted_output=bool(spec.untrusted_output),
                scope=spec.scope,
                summary_template=spec.summary_template,
            )
        raw = self.config.get("tools.risk_overrides", {}) or {}
        overrides = dict(raw) if isinstance(raw, dict) else {}
        if risk is None:
            overrides.pop(name, None)
        else:
            overrides[name] = risk
        self.config.set("tools.risk_overrides", overrides)
        save_config(self.config)
        self.events.record(
            "tools.risk_override",
            data={"tool": name, "risk": effective, "cleared": risk is None},
        )

    def unregister_module(self, module_id: str) -> list[str]:
        """Remove every tool owned by one module (reload/disable).

        Tools are registered globally and filtered per run; removing them
        here makes a disabled module immediately uncallable while a run
        snapshot simply stops disclosing names that no longer exist.
        """
        with self._lock:
            names = [
                name for name, spec in self._tools.items()
                if spec.module == module_id
            ]
            for name in names:
                self._tools.pop(name, None)
                self._base_risks.pop(name, None)
        return names

    @staticmethod
    def role_restriction(spec: ToolSpec, role: str | None) -> str | None:
        """Why this tool is unavailable to a sub-agent, or ``None``.

        The schema snapshot already hides these tools; this second check makes
        a call by name fail closed instead of bypassing the child's reduced
        toolset.
        """
        if (role or "user") != "subagent":
            return None
        if spec.name in SUBAGENT_BLOCKED_NAMES:
            return f"tool '{spec.name}' is unavailable to sub-agents"
        if spec.risk != RISK_LOW:
            return (
                f"tool '{spec.name}' is confirmation-gated and unavailable "
                "to sub-agents; report blocked instead"
            )
        return None

    def resolve(
        self, name: str, *, allowed: set[str] | frozenset[str] | None = None,
        role: str | None = None,
    ) -> ToolSpec:
        with self._lock:
            spec = self._tools.get(name)
            available = ", ".join(sorted(self._tools))
            outside_snapshot = (
                allowed is not None and name not in allowed
            )
        if spec is None:
            raise ToolError(
                f"unknown tool: {name} (available: {available})"
            )
        # Visibility is enforced twice: the schema is filtered per run, and a
        # call by name is refused when the tool is not in the run snapshot.
        # Hiding a schema while still accepting the call is a permission
        # bypass, so this check is deliberately independent of risk class.
        if outside_snapshot:
            raise ToolDenied(
                f"tool '{name}' is not available in this run"
            )
        restriction = self.role_restriction(spec, role)
        if restriction is not None:
            raise ToolDenied(restriction)
        return spec

    def resolve_frozen(
        self, name: str, *, snapshot: dict[str, Any] | None,
        role: str | None = None,
    ) -> ToolSpec:
        """Resolve a call against a run's frozen capability snapshot.

        The handler always comes from the live registry, but the description,
        parameters, risk, and trust flags come from the snapshot taken when
        the run started. A tool that has since been removed or disabled fails
        closed instead of being resurrected from the snapshot.
        """
        names = snapshot.get("names") if isinstance(snapshot, dict) else None
        in_snapshot = not isinstance(names, list) or name in names
        with self._lock:
            current = self._tools.get(name)
        if current is None:
            # A name that never existed is an unknown tool; a name the run
            # froze but the registry lost was removed after the run started.
            if not in_snapshot:
                raise ToolError(f"unknown tool: {name}")
            raise ToolError(
                f"tool '{name}' is no longer available in this run"
            )
        if not in_snapshot:
            raise ToolDenied(f"tool '{name}' is not available in this run")
        frozen = self.frozen_specs(snapshot)
        entry = frozen.get(name) if frozen is not None else None
        spec = (
            self._frozen_tool_spec(name, entry, current)
            if entry is not None else current
        )
        restriction = self.role_restriction(spec, role)
        if restriction is not None:
            raise ToolDenied(restriction)
        return spec

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._tools)

    def resolve_optional(self, name: str) -> ToolSpec | None:
        """Look up a tool without raising (used by module registration)."""
        with self._lock:
            return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        """Every registered tool in deterministic declaration order."""
        with self._lock:
            return list(self._tools.values())

    @staticmethod
    def _schema(spec: ToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI function schemas for every registered tool."""
        with self._lock:
            specs = list(self._tools.values())
        return [self._schema(spec) for spec in specs]

    @classmethod
    def freeze_spec(cls, spec: ToolSpec) -> dict[str, Any]:
        """The durable definition of one selected tool for a run snapshot."""
        frozen: dict[str, Any] = {}
        for field in FROZEN_SPEC_FIELDS:
            value = getattr(spec, field)
            if field == "parameters":
                value = util.loads(util.dumps(value), {})
            frozen[field] = value
        return frozen

    @staticmethod
    def frozen_specs(
        snapshot: dict[str, Any] | None,
    ) -> dict[str, dict[str, Any]] | None:
        """Return the snapshot's frozen per-tool definitions, if it has them.

        Old snapshots persisted only names; those return ``None`` and the
        registry's live definitions are used, exactly as before.
        """
        if not isinstance(snapshot, dict):
            return None
        entries = snapshot.get("specs")
        if not isinstance(entries, list):
            return None
        frozen: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                frozen[entry["name"]] = entry
        return frozen

    @staticmethod
    def _schema_from_frozen(entry: dict[str, Any]) -> dict[str, Any]:
        parameters = entry.get("parameters")
        return {
            "type": "function",
            "function": {
                "name": str(entry.get("name") or ""),
                "description": str(entry.get("description") or ""),
                "parameters": (
                    parameters if isinstance(parameters, dict)
                    else {"type": "object", "properties": {}}
                ),
            },
        }

    @staticmethod
    def _frozen_tool_spec(
        name: str, entry: dict[str, Any], current: ToolSpec
    ) -> ToolSpec:
        """A live handler wearing the frozen definition from a run snapshot."""
        risk = entry.get("risk")
        parameters = entry.get("parameters")
        return ToolSpec(
            name=name,
            description=str(
                entry.get("description")
                if entry.get("description") is not None
                else current.description
            ),
            parameters=(
                parameters if isinstance(parameters, dict)
                else current.parameters
            ),
            risk=risk if risk in (RISK_LOW, RISK_CONFIRM) else current.risk,
            handler=current.handler,
            module=str(entry.get("module") or current.module),
            side_effects=(
                bool(entry.get("side_effects"))
                if "side_effects" in entry else current.side_effects
            ),
            external_egress=(
                bool(entry.get("external_egress"))
                if "external_egress" in entry else current.external_egress
            ),
            read_only=(
                bool(entry.get("read_only"))
                if "read_only" in entry else current.read_only
            ),
            untrusted_output=(
                bool(entry.get("untrusted_output"))
                if "untrusted_output" in entry else current.untrusted_output
            ),
            scope=str(entry.get("scope") or current.scope),
            summary_template=str(entry.get("summary_template") or ""),
        )

    def schemas_for(
        self,
        domain_id: str | None,
        *,
        budget_tokens: int | None = None,
        eligible_modules: set[str] | frozenset[str] | None = None,
        role: str = "user",
        exclude_names: set[str] | frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Select this run's tool schemas under a token budget.

        ``eligible_modules`` is the registry's active module set for the
        task's domain. Builtins are always eligible. Ineligible module tools
        are excluded with a recorded reason, so a domain-bound module cannot
        leak into another domain. The budget drops whole tools, lowest
        priority and latest-registered first, never a partial schema.

        Returns the schemas plus the capability snapshot fields persisted on
        the run: names, modules, exclusions, token totals.
        """
        budget = max(0, int(budget_tokens or 0))
        entries = []
        exclusions: list[dict[str, Any]] = []
        excluded = set(exclude_names or ())
        with self._lock:
            live_specs = list(self._tools.values())
        for index, spec in enumerate(live_specs):
            if spec.name in excluded:
                exclusions.append({
                    "tool": spec.name,
                    "module": spec.module,
                    "risk": spec.risk,
                    "tokens": 0,
                    "reason": "disabled",
                    "detail": "disabled by configuration for this run",
                })
                continue
            restriction = self.role_restriction(spec, role)
            if restriction is not None:
                exclusions.append({
                    "tool": spec.name,
                    "module": spec.module,
                    "risk": spec.risk,
                    "tokens": 0,
                    "reason": "subagent_restricted",
                    "detail": restriction,
                })
                continue
            if (
                eligible_modules is not None
                and spec.module != "builtin"
                and spec.module not in eligible_modules
            ):
                exclusions.append({
                    "tool": spec.name,
                    "module": spec.module,
                    "risk": spec.risk,
                    "tokens": 0,
                    "reason": "module_not_eligible",
                    "detail": (
                        f"module {spec.module} is not active for this domain"
                    ),
                })
                continue
            schema = self._schema(spec)
            entries.append({
                "spec": spec,
                "index": index,
                "schema": schema,
                "tokens": util.token_len(util.dumps(schema)),
            })
        kept = list(entries)
        total = sum(entry["tokens"] for entry in kept)
        if budget > 0:
            # Drop in reverse priority: the rest, global modules, domain-bound
            # modules, trusted low-risk modules, then builtins. Within one
            # priority the most recently registered tool is dropped first,
            # which keeps the long-standing core tools stable.
            order = sorted(
                entries,
                key=lambda entry: (
                    self._schema_priority(entry["spec"]), entry["index"],
                ),
                reverse=True,
            )
            for entry in order:
                if total <= budget:
                    break
                if entry not in kept:
                    continue
                kept.remove(entry)
                total -= entry["tokens"]
                spec = entry["spec"]
                exclusions.append({
                    "tool": spec.name,
                    "module": spec.module,
                    "risk": spec.risk,
                    "tokens": entry["tokens"],
                    "reason": "tool_schema_budget",
                    "detail": (
                        f"dropped to respect the {budget}-token tool "
                        "schema budget"
                    ),
                })
        kept.sort(key=lambda entry: entry["index"])
        exclusions.sort(key=lambda item: (item["tool"], item["reason"]))
        return {
            "domain_id": domain_id,
            "role": role,
            "schemas": [entry["schema"] for entry in kept],
            "names": [entry["spec"].name for entry in kept],
            # Frozen policy travels with the schemas so the run never has to
            # read a later reload's risk/trust metadata.
            "specs": [self.freeze_spec(entry["spec"]) for entry in kept],
            "modules": sorted({entry["spec"].module for entry in kept}),
            "exclusions": exclusions,
            "tokens": total,
            "budget_tokens": budget,
        }

    @staticmethod
    def _schema_priority(spec: ToolSpec) -> int:
        """Lower is kept longer when the schema budget is tight."""
        if spec.module == "builtin":
            return 0
        if not spec.side_effects and spec.risk == RISK_LOW:
            return 1
        if spec.scope == "domain":
            return 3
        if spec.scope == "global":
            return 4
        return 5

    def schemas_for_names(self, names: list[str]) -> list[dict[str, Any]]:
        """Rebuild the schemas for a persisted run snapshot.

        Unknown names are ignored: a module may have been disabled since the
        run began, and a snapshot must not resurrect it. Kept for old name-only
        snapshots; new snapshots use :meth:`schemas_for_snapshot`.
        """
        wanted = set(names)
        return [
            self._schema(spec)
            for spec in self.specs()
            if spec.name in wanted
        ]

    def schemas_for_snapshot(
        self, snapshot: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Rebuild a run's schemas from its frozen definitions.

        A tool that has since been removed is dropped (the same behaviour the
        name-only form always had); every surviving tool keeps the exact
        description and parameters it had when the run started. An old
        snapshot without frozen specs falls back to the live definitions.
        """
        names = snapshot.get("names") if isinstance(snapshot, dict) else None
        if not isinstance(names, list):
            return []
        frozen = self.frozen_specs(snapshot)
        if frozen is None:
            return self.schemas_for_names(names)
        live = set(self.names())
        schemas: list[dict[str, Any]] = []
        for name in names:
            if name not in live:
                continue
            entry = frozen.get(name)
            if entry is not None:
                schemas.append(self._schema_from_frozen(entry))
                continue
            spec = self.resolve_optional(name)
            if spec is not None:
                schemas.append(self._schema(spec))
        return schemas

    # -- execution --------------------------------------------------------

    def validate(
        self, call: ToolCall, *,
        allowed: set[str] | frozenset[str] | None = None,
        role: str | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Public validation used by the runtime before creating approvals.

        With ``snapshot`` the arguments are validated against that run's frozen
        parameters, never a later reload's definition.
        """
        spec = (
            self.resolve_frozen(call.name, snapshot=snapshot, role=role)
            if snapshot is not None
            else self.resolve(call.name, allowed=allowed, role=role)
        )
        validated = validate_arguments(spec, call.arguments)
        if spec.name == "create_domain":
            validate_create_domain_arguments(validated)
        return validated

    def record_rejected(
        self, call: ToolCall, *, run_id: str, task_id: str, error: str
    ) -> None:
        """Record a call that never dispatched (unknown tool, bad name)."""
        self.db.execute(
            "INSERT INTO tool_executions(id, run_id, task_id, tool,"
            " tool_call_id, arguments, status, started_at, ended_at, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                util.new_id("ex"), run_id, task_id, call.name, call.id,
                call.raw_arguments, "denied", util.iso(), util.iso(), error,
            ),
        )
        self.events.record(
            "tool.rejected", level="warn", task_id=task_id, run_id=run_id,
            data={"tool": call.name, "error": error},
        )

    def execute(
        self,
        call: ToolCall,
        ctx: ToolContext,
        *,
        run_id: str,
        task_id: str,
        execution_id: str | None = None,
        approval_id: str | None = None,
        allowed_names: set[str] | frozenset[str] | None = None,
        role: str | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate + dispatch + record. Never raises for tool-level problems;
        those are captured as status='failed'/'denied'. When ``execution_id``
        is given (an approval resumed), the existing record is completed in
        place instead of creating a duplicate row. ``allowed_names`` is the
        run's capability snapshot; a name outside it is denied, not run.
        ``snapshot`` additionally freezes the schema and policy used here."""
        started = util.iso()
        started_monotonic = time.monotonic()
        execution_id = execution_id or util.new_id("ex")
        try:
            if snapshot is not None:
                spec = self.resolve_frozen(
                    call.name, snapshot=snapshot, role=role
                )
            else:
                spec = self.resolve(
                    call.name, allowed=allowed_names, role=role
                )
            arguments = self.validate(
                call, allowed=allowed_names, role=role, snapshot=snapshot
            )
        except (ToolError, ToolDenied) as exc:
            self._record(
                execution_id, run_id, task_id, call, "denied", started,
                started_monotonic, None, str(exc), approval_id,
            )
            return {"status": "denied", "error": str(exc)}

        # Persist the attempt before the handler runs. If the process dies
        # inside the handler, recovery turns this row into an explicit
        # unknown outcome instead of losing the fact that it was attempted.
        existing = self.db.query_one(
            "SELECT status FROM tool_executions WHERE id=?", (execution_id,)
        )
        if existing is not None:
            if existing["status"] not in ("awaiting_approval", "in_flight"):
                return {
                    "status": "denied",
                    "error": f"execution {execution_id} is already "
                             f"{existing['status']}; refusing to run it twice",
                }
            if existing["status"] == "awaiting_approval":
                self.db.execute(
                    "UPDATE tool_executions SET status='in_flight',"
                    " started_at=? WHERE id=? AND status='awaiting_approval'",
                    (started, execution_id),
                )
        else:
            self.db.execute(
                "INSERT INTO tool_executions(id, run_id, task_id, tool,"
                " tool_call_id, arguments, status, started_at, approval_id)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    execution_id, run_id, task_id, call.name, call.id,
                    call.raw_arguments, "in_flight", started, approval_id,
                ),
            )

        status = "failed"
        result: Any = None
        error: str | None = None
        try:
            result = spec.handler(ctx, arguments)
            status = "succeeded"
        except MemoryError as exc:
            error = str(exc)
        except Exception as exc:  # tool bugs must not kill the runtime
            error = f"{type(exc).__name__}: {exc}"
        if status != "succeeded":
            self._record(
                execution_id, run_id, task_id, call, status, started,
                started_monotonic, None, error, approval_id,
            )
            return {"status": status, "error": error}
        bounded = util.clip(util.dumps(result), int(
            self.config.get("limits.max_tool_result_chars", 6000)
        ))
        parsed = util.loads(bounded, None)
        # A clipped JSON blob may not parse; never fall back to the full
        # unbounded result when it does not.
        record_value = parsed if parsed is not None else {
            "trimmed": True, "preview": bounded,
        }
        self._record(
            execution_id, run_id, task_id, call, status, started,
            started_monotonic, record_value, None, approval_id,
        )
        return {"status": "succeeded", "result": result,
                "execution_id": execution_id}

    def _record(
        self,
        execution_id: str,
        run_id: str,
        task_id: str,
        call: ToolCall,
        status: str,
        started: str,
        started_monotonic: float,
        result: Any,
        error: str | None,
        approval_id: str | None = None,
    ) -> None:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        result_json = util.dumps(result) if result is not None else None
        existing = self.db.query_one(
            "SELECT id FROM tool_executions WHERE id=?", (execution_id,)
        )
        if existing:
            self.db.execute(
                "UPDATE tool_executions SET status=?, ended_at=?, result=?,"
                " error=?, approval_id=?, duration_ms=? WHERE id=?",
                (
                    status, util.iso(), result_json, error, approval_id,
                    duration_ms, execution_id,
                ),
            )
        else:
            self.db.execute(
                "INSERT INTO tool_executions(id, run_id, task_id, tool,"
                " tool_call_id, arguments, status, started_at, ended_at,"
                " result, error, approval_id, duration_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    execution_id, run_id, task_id, call.name, call.id,
                    call.raw_arguments, status, started, util.iso(),
                    result_json, error, approval_id, duration_ms,
                ),
            )
        self.events.record(
            "tool.executed" if status == "succeeded" else "tool.failed",
            level="info" if status == "succeeded" else "warn",
            task_id=task_id, run_id=run_id,
            data={"tool": call.name, "status": status, "error": error,
                  "execution_id": execution_id},
        )

    def executions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self.db.query(
                "SELECT * FROM tool_executions WHERE run_id=? ORDER BY started_at",
                (run_id,),
            )
        )

    # -- builtins ---------------------------------------------------------

    def _register_builtins(self) -> None:
        self.register(ToolSpec(
            name="remember",
            description=(
                "Store durable information for later recall. Use for facts, "
                "user preferences, decisions, procedures, constraints, and "
                "open questions the user asks you to remember or that are "
                "clearly worth keeping. Durable facts about the user "
                "(preferences, constraints, identity) default to global; "
                "facts about the current domain use that domain's scope. Do "
                "not save transient "
                "chatter."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The information to remember, one clear statement.",
                    },
                    "kind": {
                        "type": "string",
                        "description": "fact | preference | decision | "
                            "procedure | constraint | open_question | context | temp",
                    },
                    "scope": {
                        "type": "string",
                        "description": "'global' or 'domain:<id>'. Defaults to "
                            "the current domain, or global from General.",
                    },
                    "detail": {
                        "type": "string",
                        "description": "Optional longer note stored as a "
                            "markdown artifact.",
                    },
                },
                "required": ["content"],
            },
            risk=RISK_LOW,
            # Durable write: a tainted run must not store untrusted content
            # without an explicit approval.
            side_effects=True,
            handler=self._tool_remember,
        ))
        self.register(ToolSpec(
            name="update_memory",
            description=(
                "Correct or update an existing memory by id. Creates a "
                "revision; the old version stays inspectable."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "content": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["memory_id", "content"],
            },
            risk=RISK_LOW,
            # Durable write to an existing memory (a new revision).
            side_effects=True,
            handler=self._tool_update_memory,
        ))
        self.register(ToolSpec(
            name="retract_memory",
            description="Remove a memory that is wrong or no longer useful.",
            parameters={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["memory_id", "reason"],
            },
            risk=RISK_LOW,
            # Durable write: retraction is a status change on the memory row.
            side_effects=True,
            handler=self._tool_retract_memory,
        ))
        self.register(ToolSpec(
            name="search_knowledge",
            description=(
                "Search memories and older conversation history beyond what "
                "is already in context. Use when you suspect a relevant fact, "
                "decision, or prior discussion exists but is not visible."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search terms or a question.",
                    },
                    "include_archived": {
                        "type": "boolean",
                        "description": "Also search archived memories without "
                            "restoring them; archived hits are labelled.",
                    },
                    "restore_archived": {
                        "type": "boolean",
                        "description": "Restore archived hits. Requires "
                            "include_archived=true; otherwise hits are read-only.",
                    },
                },
                "required": ["query"],
            },
            risk=RISK_LOW,
            handler=self._tool_search_knowledge,
        ))
        self.register(ToolSpec(
            name="read_artifact",
            description=(
                "Read a stored note/artifact by its path (as referenced by a "
                "memory or listed in context). Reads are bounded to the "
                "artifacts directory."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            risk=RISK_LOW,
            # Artifacts can contain delegated or externally derived text.
            untrusted_output=True,
            handler=self._tool_read_artifact,
        ))
        self.register(ToolSpec(
            name="update_task_notes",
            description=(
                "Record the plan, decisions, and current step for the active "
                "task so work can resume after a pause or restart. Keep it "
                "short and factual."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "notes": {"type": "string"},
                },
                "required": ["notes"],
            },
            risk=RISK_LOW,
            # Notes are durable prompt state. Untrusted-derived notes require
            # the same explicit approval as other persistent writes.
            side_effects=True,
            handler=self._tool_update_task_notes,
        ))
        self.register(ToolSpec(
            name="create_domain",
            description=(
                "Create a new domain: a durable, distinct area of work that "
                "warrants its own knowledge (not a one-off topic). Requires "
                "explicit user approval; state why it is needed. If the "
                "domain already exists this returns it without creating or "
                "asking again."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "reason": {"type": "string"},
                    "keywords": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                        "description": (
                            "At least one short routing term that should select "
                            "this domain."
                        ),
                    },
                    "examples": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                        "description": (
                            "At least one example user phrase that should "
                            "select this domain."
                        ),
                    },
                },
                "required": [
                    "name", "description", "reason", "keywords", "examples",
                ],
            },
            risk=RISK_CONFIRM,  # the user must approve the exact creation
            handler=self._tool_create_domain,
        ))
        self.register(ToolSpec(
            name="use_domain",
            description=(
                "Move the current task to an existing domain. Use this when "
                "the work clearly belongs somewhere else. It never creates a "
                "domain and never changes permissions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Existing domain id or exact name.",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["domain"],
            },
            risk=RISK_LOW,
            handler=self._tool_use_domain,
        ))
        self.register(ToolSpec(
            name="edit_domain",
            description=(
                "Propose a change to user-authored domain instructions. The "
                "full bounded text requires explicit approval before it is "
                "written, and this tool is unavailable in a run that consumed "
                "untrusted content."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Existing active domain id or exact name.",
                    },
                    "instructions": {
                        "type": "string",
                        "description": (
                            "Full replacement domain instructions, bounded by "
                            "domains.instructions_max_chars."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why the instruction change is needed.",
                    },
                },
                "required": ["domain", "instructions", "reason"],
            },
            risk=RISK_CONFIRM,
            # Durable user-authored state: approval is mandatory.
            side_effects=True,
            handler=self._tool_edit_domain,
        ))
        self.register(ToolSpec(
            name="schedule_task",
            description=(
                "Create a durable schedule that submits a prompt to the normal "
                "runtime later. Use only for recurring or delayed work the "
                "user wants. It requires approval, is bound to this task, and "
                "is capped by scheduler.max_schedules_per_task and "
                "scheduler.max_schedule_depth."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short schedule name.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Instruction submitted when due.",
                    },
                    "kind": {
                        "type": "string",
                        "description": "once | interval | daily | weekly",
                    },
                    "at_ts": {
                        "type": "string",
                        "description": "For once: ISO date/time.",
                    },
                    "interval_s": {
                        "type": "number",
                        "description": "For interval: seconds between runs.",
                    },
                    "time_of_day": {
                        "type": "string",
                        "description": "For daily/weekly: HH:MM local time.",
                    },
                    "weekday": {
                        "type": "string",
                        "description": "For weekly: mon..sun (or a full name).",
                    },
                    "tz": {
                        "type": "string",
                        "description": "System or IANA timezone. Defaults to "
                            "the configured timezone.",
                    },
                },
                "required": ["name", "prompt", "kind"],
            },
            risk=RISK_CONFIRM,
            handler=self._tool_schedule_task,
        ))
        self.register(ToolSpec(
            name="cancel_schedule",
            description=(
                "Cancel a schedule previously created by this task. It never "
                "cancels a schedule owned by another task. Requires approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["schedule_id"],
            },
            risk=RISK_CONFIRM,
            handler=self._tool_cancel_schedule,
        ))
        self.register(ToolSpec(
            name="delegate_task",
            description=(
                "Delegate one bounded sub-problem to a sequential sub-agent "
                "and receive its result. Use for self-contained research, "
                "summarisation, or drafting work where the parent does not "
                "need to see every step. The child cannot ask the user, "
                "create domains, or perform approval-gated actions; it "
                "returns blocked instead. Do not delegate the whole task."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "One concrete, self-contained goal.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Background the child needs. The "
                            "parent transcript is never shared.",
                    },
                    "expected_output": {
                        "type": "string",
                        "description": "Shape/length of the result wanted.",
                    },
                },
                "required": ["goal"],
            },
            risk=RISK_LOW,
            handler=self._tool_delegate_task,
        ))
        self.register(ToolSpec(
            name="propose_skill",
            description=(
                "Propose a reusable procedural skill for the user to review. "
                "A skill is instruction text only: no code, handlers, or tool "
                "schemas. The proposal becomes a draft only after explicit "
                "approval, and drafts are never activated automatically. "
                "Reusing the exact name of an existing skill you created "
                "revises it; imported and user-created skills cannot be "
                "revised this way."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short reusable procedure name.",
                    },
                    "description": {
                        "type": "string",
                        "description": "One line describing when it applies.",
                    },
                    "preconditions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Short keywords that indicate when "
                            "the skill applies.",
                    },
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Existing domain ids to bind to; [] "
                            "means a global/adaptive skill.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Instructions only, bounded by "
                            "skills.max_body_chars.",
                    },
                },
                "required": ["name", "description", "body"],
            },
            risk=RISK_CONFIRM,
            # Durable cross-run state: approving writes a draft skill row.
            # This also keeps skill writes out of "approve all safe" and hides
            # the always-allow control, which cannot apply to skill writes.
            side_effects=True,
            handler=self._tool_propose_skill,
        ))
        self.register(ToolSpec(
            name="edit_skill",
            description=(
                "Refine a skill the agent created. Use the existing skill "
                "name; omitted fields keep their current values. A skill is "
                "instruction text only, and the edit becomes a new draft "
                "version only after explicit approval. Imported and "
                "user-created skills cannot be edited by the agent."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact name of an agent-created "
                            "skill.",
                    },
                    "description": {
                        "type": "string",
                        "description": "New one-line description; omit to "
                            "keep the current one.",
                    },
                    "preconditions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New precondition keywords; omit to "
                            "keep the current list.",
                    },
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New domain bindings; omit to keep "
                            "the current bindings.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Full replacement instructions, "
                            "bounded by skills.max_body_chars; omit to keep "
                            "the current body.",
                    },
                },
                "required": ["name"],
            },
            risk=RISK_CONFIRM,
            # Durable cross-run state: approving writes a new draft version.
            side_effects=True,
            handler=self._tool_edit_skill,
        ))

    # handlers -------------------------------------------------------------

    def _tool_remember(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        assert ctx.memory and ctx.domains
        provenance = {"run_id": ctx.run_id, "task_id": ctx.task_id}
        if ctx.tainted_origin:
            # This run consumed untrusted content; the memory row must say so.
            provenance["tainted"] = True
            provenance["tainted_origin"] = ctx.tainted_origin
        out = ctx.memory.remember(
            args["content"],
            kind=args.get("kind"),
            scope=args.get("scope"),
            domain_id=ctx.domain_id,
            origin="agent" if ctx.task_id else "user",
            provenance=provenance,
            domains=ctx.domains,
            artifact_body=args.get("detail"),
            task_id=ctx.task_id,
            run_id=ctx.run_id,
            origin_domain_id=ctx.domain_id or "general",
        )
        mem = out["memory"]
        return {
            "memory_id": mem["id"],
            "kind": mem["kind"],
            "scope": mem["scope"],
            "saved": util.clip(mem["content"], 400),
            "duplicate": out["duplicate"],
            "superseded_id": None,
            "possible_conflict_id": (
                (out.get("possible_conflict") or {}).get("id")
            ),
        }

    def _tainted_reason(self, ctx: ToolContext, reason: str | None) -> str:
        """Mark a revision reason with the run's untrusted origin.

        The revision reason is what the UI shows, so the taint must be
        visible there, not only in internal records.
        """
        base = (reason or "").strip()
        tail = f"[provenance: untrusted content from {ctx.tainted_origin}]"
        return f"{base} {tail}" if base else tail

    def _record_tainted_write(
        self, ctx: ToolContext, tool: str, memory_id: str
    ) -> None:
        """Audit trail for a memory write that happened in a tainted run."""
        self.events.record(
            "memory.write_tainted", task_id=ctx.task_id, run_id=ctx.run_id,
            data={
                "tool": tool,
                "memory_id": memory_id,
                "origin": util.clip(ctx.tainted_origin, 300),
            },
        )

    def _tool_update_memory(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        assert ctx.memory
        reason = args.get("reason")
        if ctx.tainted_origin:
            reason = self._tainted_reason(ctx, reason)
        mem = ctx.memory.update(
            args["memory_id"], content=args["content"],
            changed_by="agent", reason=reason,
            tainted_origin=ctx.tainted_origin or None,
        )
        if ctx.tainted_origin:
            self._record_tainted_write(ctx, "update_memory", mem["id"])
        return {"memory_id": mem["id"], "revision": mem["revision"]}

    def _tool_retract_memory(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        assert ctx.memory
        reason = args["reason"]
        if ctx.tainted_origin:
            reason = self._tainted_reason(ctx, reason)
        mem = ctx.memory.retract(
            args["memory_id"], changed_by="agent", reason=reason,
            tainted_origin=ctx.tainted_origin or None,
        )
        if ctx.tainted_origin:
            self._record_tainted_write(ctx, "retract_memory", mem["id"])
        return {"memory_id": mem["id"], "status": mem["status"]}

    def _tool_search_knowledge(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        assert ctx.memory and ctx.messages and ctx.domains
        query = args["query"]
        priority: list[str] = []
        if ctx.domain_id and ctx.domain_id != "general":
            priority.append(f"domain:{ctx.domain_id}")
        priority.append("global")
        mode = str(self.config.get("memory.cross_domain", "off") or "off")
        # Search mode changes only this explicit tool: context remains scoped.
        # Off and boost retain the current-domain-then-global tool surface.
        include_archived = bool(args.get("include_archived"))
        restore_archived = bool(args.get("restore_archived"))
        if restore_archived and not include_archived:
            raise ToolError(
                "restore_archived requires include_archived=true"
            )
        memories = ctx.memory.search(
            query, scope_priority=priority,
            include_archived=include_archived,
            restore_archived=restore_archived,
            cross_domain=(mode == "search"),
        )
        # Explicitly returned memories count as used; this is the one bump
        # per run, deduped by the memory manager. Candidate scans do not bump.
        ctx.memory.record_access(
            [m["id"] for m in memories], run_id=ctx.run_id
        )

        def origin(memory: dict[str, Any]) -> str:
            if memory.get("cross_domain"):
                domain_id = str(memory.get("origin_domain_id") or "")
                domain = ctx.domains.get(domain_id)
                return str((domain or {}).get("name") or domain_id)
            return str(
                memory.get("effective_scope") or memory.get("scope") or "global"
            )

        history = ctx.messages.search(query, limit=int(
            self.config.get("context.history_results", 4)
        ), exclude_task=ctx.task_id)
        untrusted_sources: list[dict[str, str]] = []
        seen_sources: set[tuple[str, str]] = set()

        def add_source(
            tool: str, module: str, origin: Any, *, derived: bool = False
        ) -> None:
            text = util.clip(str(origin or "untrusted provenance"), 300)
            key = (tool, text)
            if key in seen_sources:
                return
            seen_sources.add(key)
            untrusted_sources.append({
                "tool": tool, "module": module, "origin": text,
                "at": util.iso(), "derived": derived,
            })

        for memory in memories:
            provenance = memory.get("provenance") or {}
            if isinstance(provenance, dict) and provenance.get("tainted"):
                add_source(
                    f"memory:{memory['id']}", "memory",
                    provenance.get("tainted_origin"),
                )
        for message in history:
            meta = util.loads(message.get("meta") or "{}", {})
            if isinstance(meta, dict) and (
                meta.get("tainted") or meta.get("derived_untrusted")
            ):
                add_source(
                    f"message:{message['id']}", "history",
                    meta.get("tainted_origin"),
                    derived=bool(meta.get("derived_untrusted")),
                )
        return {
            "untrusted_sources": untrusted_sources,
            "memories": [
                {
                    "id": m["id"], "kind": m["kind"], "scope": m["scope"],
                    "content": util.clip(m["content"], 500),
                    "source": "memory",
                    "archived": bool(m.get("archived")),
                    "restored": bool(m.get("restored")),
                    "access_count": m.get("access_count", 0),
                    "last_accessed_at": m.get("last_accessed_at"),
                    "origin": origin(m),
                    "from": origin(m),
                }
                for m in memories
            ],
            "history": [
                {
                    "id": m["id"], "role": m["role"],
                    "content": util.clip(m["content"], 400),
                    "ts": m["ts"], "source": f"message:{m['id']}",
                }
                for m in history
            ],
            "note": "results are bounded; refine the query if incomplete",
        }

    def _tool_read_artifact(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        assert ctx.memory
        body = ctx.memory.read_artifact(args["path"])
        return {"path": args["path"], "content": body}

    def _tool_update_task_notes(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        assert ctx.tasks
        meta = ctx.tasks.meta(ctx.task_id)
        meta["notes"] = util.clip(args["notes"], 2000)
        meta["notes_run_id"] = ctx.run_id
        if ctx.tainted_origin:
            origin = util.clip(ctx.tainted_origin, 300)
            meta["notes_tainted"] = True
            meta["notes_tainted_origin"] = origin
            source = {
                "tool": "update_task_notes", "module": "builtin",
                "origin": origin, "at": util.iso(),
            }
            existing = [
                item for item in (meta.get("tainted_sources") or [])
                if isinstance(item, dict)
                and not (
                    item.get("tool") == source["tool"]
                    and item.get("origin") == source["origin"]
                )
            ]
            meta["tainted_sources"] = [*existing, source][-20:]
        ctx.tasks.set_meta(ctx.task_id, meta)
        ctx.tasks.touch(ctx.task_id)
        return {"task_id": ctx.task_id, "notes_saved": True}

    def _tool_create_domain(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        assert ctx.domains
        validate_create_domain_arguments(args)
        name = args["name"].strip()
        existing = ctx.domains.by_name(name)
        if existing:
            return {
                "already_exists": existing["name"],
                "domain_id": existing["id"],
                "note": "nothing was created; the domain already exists",
            }
        domain_id = util.new_id("dom")
        domain = ctx.domains.create(
            domain_id, name=name,
            description=args.get("description", ""),
            created_by="model", seed_note=args.get("description") or None,
            keywords=list(args.get("keywords") or []),
            examples=list(args.get("examples") or []),
        )
        return {"domain_id": domain["id"], "name": domain["name"]}

    def _tool_use_domain(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        assert ctx.domains and ctx.tasks
        wanted = str(args["domain"]).strip()
        domain = ctx.domains.get(wanted) or ctx.domains.by_name(wanted)
        if domain is None:
            raise ToolError(
                f"no such domain: {wanted}; use an existing domain id or name"
            )
        if domain["status"] != "active":
            raise ToolError(f"domain {domain['name']} is archived")
        previous = ctx.tasks.get(ctx.task_id)
        previous_domain = previous["domain_id"] if previous else None
        ctx.tasks.set_domain(ctx.task_id, domain["id"])
        ctx.domain_id = domain["id"]
        briefing: dict[str, Any] = {}
        if ctx.briefing is not None:
            disclosed = ctx.briefing(domain["id"])
            if isinstance(disclosed, dict):
                briefing = disclosed
        return {
            "task_id": ctx.task_id,
            "domain_id": domain["id"],
            "name": domain["name"],
            "previous_domain_id": previous_domain,
            "reason": args.get("reason") or "",
            "briefing": str(briefing.get("text") or ""),
            "memories": list(briefing.get("memories") or []),
            "skills": list(briefing.get("skills") or []),
            "new_tools": list(briefing.get("new_tools") or []),
        }

    def _tool_edit_domain(
        self, ctx: ToolContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        assert ctx.domains
        wanted = str(args["domain"]).strip()
        domain = ctx.domains.get(wanted) or ctx.domains.by_name(wanted)
        if domain is None:
            raise ToolError(
                f"no such domain: {wanted}; use an existing domain id or name"
            )
        if domain["status"] != "active":
            raise ToolError(f"domain {domain['name']} is archived")
        updated = ctx.domains.update(
            domain["id"], instructions=str(args["instructions"])
        )
        return {
            "domain_id": updated["id"],
            "name": updated["name"],
            "instructions": updated["instructions"],
        }

    def _tool_schedule_task(
        self, ctx: ToolContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        assert ctx.scheduler is not None
        schedule = ctx.scheduler.create_for_task(
            ctx.task_id,
            run_id=ctx.run_id,
            name=args["name"],
            prompt=args["prompt"],
            kind=args["kind"],
            at_ts=args.get("at_ts"),
            interval_s=args.get("interval_s"),
            time_of_day=args.get("time_of_day"),
            weekday=args.get("weekday"),
            tz=args.get("tz"),
        )
        return {
            "schedule_id": schedule["id"],
            "name": schedule["name"],
            "kind": schedule["kind"],
            "next_due_at": schedule["next_due_at"],
            "enabled": bool(schedule["enabled"]),
        }

    def _tool_cancel_schedule(
        self, ctx: ToolContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        assert ctx.scheduler is not None
        schedule = ctx.scheduler.cancel_for_task(
            ctx.task_id, args["schedule_id"], reason=args.get("reason")
        )
        return {
            "schedule_id": schedule["id"],
            "name": schedule["name"],
            "enabled": bool(schedule["enabled"]),
            "cancelled": True,
        }

    def _tool_delegate_task(
        self, ctx: ToolContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        assert ctx.subagents is not None
        return ctx.subagents.delegate(ctx, args)

    def _tool_propose_skill(
        self, ctx: ToolContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        assert ctx.skills is not None
        module = ctx.skills.create_draft(
            name=args["name"],
            description=args["description"],
            preconditions=args.get("preconditions"),
            domains=args.get("domains"),
            body=args["body"],
            origin="agent",
            provenance={
                "source": "model",
                "run_id": ctx.run_id,
                "task_id": ctx.task_id,
            },
            created_by="model",
            reason="proposed by the model and approved by the user",
        )
        return {
            "skill_id": module.id,
            "name": module.name,
            "version": module.version,
            "status": module.status,
            "scope": module.scope,
            "note": "saved as a draft; activate it explicitly when ready",
        }

    def _tool_edit_skill(
        self, ctx: ToolContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        assert ctx.skills is not None
        module = ctx.skills.edit(
            name=args["name"],
            description=args.get("description"),
            preconditions=args.get("preconditions"),
            domains=args.get("domains"),
            body=args.get("body"),
            created_by="model",
            provenance={
                "source": "model",
                "run_id": ctx.run_id,
                "task_id": ctx.task_id,
                "edited": True,
            },
            reason="refined by the model and approved by the user",
        )
        return {
            "skill_id": module.id,
            "name": module.name,
            "version": module.version,
            "status": module.status,
            "scope": module.scope,
            "note": "saved as a draft revision; activate it explicitly when "
                    "ready",
        }


# ---------------------------------------------------------------------------
# MCP: minimal stdio JSON-RPC client
# ---------------------------------------------------------------------------

class McpError(RuntimeError):
    pass


class McpServer:
    """One configured stdio MCP server. Launched per call for isolation."""

    def __init__(self, name: str, command: str, args: list[str],
                 env: dict[str, str] | None = None,
                 timeout_s: float = 30.0):
        self.name = name
        self.command = command
        self.args = args
        self.env = env
        self.timeout_s = max(0.1, float(timeout_s or 30.0))

    def _request(
        self,
        process: subprocess.Popen,
        payload: dict[str, Any],
        stderr: Any = None,
        lines: "queue.Queue[Any] | None" = None,
    ) -> dict[str, Any]:
        stdin_text = util.dumps(payload) + "\n"
        process.stdin.write(stdin_text.encode("utf-8"))
        process.stdin.flush()
        # Read lines until a response with our id arrives (skip notifications).
        # A reader thread feeds the queue because a blocking pipe readline()
        # would otherwise ignore the timeout: a server that starts and never
        # answers must fail the call instead of hanging the caller forever.
        if lines is None:
            lines = queue.Queue()
            threading.Thread(
                target=self._pump_stdout,
                args=(process.stdout, lines),
                daemon=True,
                name=f"mcp-read-{self.name}",
            ).start()
        deadline = time.monotonic() + self.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(self._failure("timed out", stderr))
            try:
                item = lines.get(timeout=remaining)
            except queue.Empty:
                raise McpError(self._failure("timed out", stderr)) from None
            if isinstance(item, BaseException):
                raise McpError(self._failure(f"read failed: {item}", stderr))
            if not item:
                raise McpError(self._failure(
                    "closed its output", stderr
                ))
            text = item.decode("utf-8", errors="replace")
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if message.get("id") == payload.get("id"):
                if "error" in message:
                    raise McpError(
                        f"MCP server '{self.name}' error: "
                        f"{message['error'].get('message')}"
                    )
                return message.get("result") or {}

    @staticmethod
    def _pump_stdout(stream: Any, lines: "queue.Queue[Any]") -> None:
        """Feed stdout lines to a queue until EOF; never raises."""
        try:
            while True:
                line = stream.readline()
                lines.put(line)
                if not line:
                    return
        except Exception as exc:  # a broken pipe during shutdown
            lines.put(exc)

    def _failure(self, reason: str, stderr: Any = None) -> str:
        detail = self._stderr_text(stderr)
        message = f"MCP server '{self.name}' {reason}"
        return f"{message}: {detail}" if detail else message

    @staticmethod
    def _stderr_text(stderr: Any, limit: int = 800) -> str:
        """Bounded stderr tail, so a crash is diagnosable from the UI."""
        if stderr is None:
            return ""
        try:
            stderr.flush()
            stderr.seek(0, 2)
            size = stderr.tell()
            stderr.seek(max(0, size - limit * 4))
            text = stderr.read(limit * 4).decode(
                "utf-8", errors="replace"
            ).strip()
        except Exception:
            return ""
        return text[-limit:]

    def _launch(self) -> tuple[subprocess.Popen, Any, "queue.Queue[Any]"]:
        # A temp file (not a pipe) avoids a deadlock when a server logs a lot
        # to stderr, while still letting a failure report the real reason.
        stderr = tempfile.TemporaryFile()
        try:
            process = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=stderr,
                env=mcp_process_env(self.env),
            )
        except OSError as exc:
            stderr.close()
            raise McpError(f"cannot start MCP server '{self.name}': {exc}") from exc
        lines: "queue.Queue[Any]" = queue.Queue()
        threading.Thread(
            target=self._pump_stdout,
            args=(process.stdout, lines),
            daemon=True,
            name=f"mcp-read-{self.name}",
        ).start()
        return process, stderr, lines

    def list_tools(self) -> list[dict[str, Any]]:
        process, stderr, lines = self._launch()
        try:
            self._request(process, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "harness", "version": "2.0.0"},
                },
            }, stderr, lines)
            process.stdin.write((util.dumps({
                "jsonrpc": "2.0", "method": "notifications/initialized"
            }) + "\n").encode("utf-8"))
            process.stdin.flush()
            result = self._request(process, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            }, stderr, lines)
            return result.get("tools") or []
        finally:
            self._shutdown(process, stderr)

    def probe(self) -> dict[str, Any]:
        """List tools and return a bounded stderr tail for a UI preview.

        This uses the same initialize/initialized/tools-list sequence as
        :meth:`list_tools`, but keeps stderr available to the caller. The
        subprocess is always shut down before returning or raising.
        """
        process, stderr, lines = self._launch()
        try:
            self._request(process, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "harness", "version": "2.0.0"},
                },
            }, stderr, lines)
            process.stdin.write((util.dumps({
                "jsonrpc": "2.0", "method": "notifications/initialized"
            }) + "\n").encode("utf-8"))
            process.stdin.flush()
            result = self._request(process, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            }, stderr, lines)
            tools = result.get("tools") or []
            return {
                "tools": tools,
                "stderr": self._stderr_text(stderr),
            }
        except Exception as exc:
            # The preview endpoint can report a bounded diagnostic without
            # changing list_tools/call error behaviour.
            try:
                setattr(exc, "stderr_tail", self._stderr_text(stderr))
            except Exception:
                pass
            raise
        finally:
            self._shutdown(process, stderr)

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        process, stderr, lines = self._launch()
        try:
            self._request(process, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "harness", "version": "2.0.0"},
                },
            }, stderr, lines)
            process.stdin.write((util.dumps({
                "jsonrpc": "2.0", "method": "notifications/initialized"
            }) + "\n").encode("utf-8"))
            process.stdin.flush()
            result = self._request(process, {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }, stderr, lines)
            if result.get("isError"):
                texts = [
                    item.get("text", "") for item in result.get("content") or []
                    if isinstance(item, dict)
                ]
                raise McpError("; ".join(t for t in texts if t) or "MCP tool error")
            return result
        finally:
            self._shutdown(process, stderr)

    def _shutdown(self, process: subprocess.Popen, stderr: Any = None) -> None:
        try:
            process.stdin.close()
            process.wait(timeout=5)
        except Exception:
            process.kill()
            try:
                process.wait(timeout=5)
            except Exception:
                pass
        finally:
            if stderr is not None:
                try:
                    stderr.close()
                except Exception:
                    pass


def mcp_text(result: dict[str, Any], limit: int | None = 4000) -> str:
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    text = "\n".join(parts)
    return text if limit is None else text[:limit]


# MCP module loading lives in ``harness/modules/mcp.py``: it is registry-backed
# so discovery, health, domain bindings, and reload share one record shape.
# The stdio client above stays here because it is the transport, not policy.

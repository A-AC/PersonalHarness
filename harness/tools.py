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
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import util
from .db import Database, rows_to_dicts
from .memory import MemoryError, MemoryManager
from .model import ToolCall
from .store import Domains, Events, Messages, Tasks

RISK_LOW = "low"
RISK_CONFIRM = "confirm"

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
    read_only: bool = False
    untrusted_output: bool = False
    # Eligibility scope of the owning module (global | domain | adaptive).
    scope: str = "global"


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
    role: str = "user"


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
        self._register_builtins()

    # -- registration -----------------------------------------------------

    def register(self, spec: ToolSpec) -> None:
        if _BANNED.match(spec.name):
            raise ToolDenied(
                f"refusing to register tool '{spec.name}': shell/command "
                "execution tools are not permitted"
            )
        if spec.risk not in (RISK_LOW, RISK_CONFIRM):
            raise ToolError(f"invalid risk class for {spec.name}: {spec.risk}")
        overrides = self.config.get("tools.risk_overrides", {}) or {}
        effective = overrides.get(spec.name, spec.risk)
        if effective not in (RISK_LOW, RISK_CONFIRM):
            raise ToolError(f"invalid risk override for {spec.name}")
        self._tools[spec.name] = ToolSpec(
            name=spec.name, description=spec.description,
            parameters=spec.parameters, risk=effective, handler=spec.handler,
            module=spec.module, side_effects=bool(spec.side_effects),
            read_only=bool(spec.read_only),
            untrusted_output=bool(spec.untrusted_output),
            scope=spec.scope,
        )

    def unregister_module(self, module_id: str) -> list[str]:
        """Remove every tool owned by one module (reload/disable).

        Tools are registered globally and filtered per run; removing them
        here makes a disabled module immediately uncallable while a run
        snapshot simply stops disclosing names that no longer exist.
        """
        names = [
            name for name, spec in self._tools.items()
            if spec.module == module_id
        ]
        for name in names:
            self._tools.pop(name, None)
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
        spec = self._tools.get(name)
        if spec is None:
            raise ToolError(
                f"unknown tool: {name} (available: "
                f"{', '.join(sorted(self._tools))})"
            )
        # Visibility is enforced twice: the schema is filtered per run, and a
        # call by name is refused when the tool is not in the run snapshot.
        # Hiding a schema while still accepting the call is a permission
        # bypass, so this check is deliberately independent of risk class.
        if allowed is not None and name not in allowed:
            raise ToolDenied(
                f"tool '{name}' is not available in this run"
            )
        restriction = self.role_restriction(spec, role)
        if restriction is not None:
            raise ToolDenied(restriction)
        return spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def resolve_optional(self, name: str) -> ToolSpec | None:
        """Look up a tool without raising (used by module registration)."""
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        """Every registered tool in deterministic declaration order."""
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
        return [self._schema(spec) for spec in self._tools.values()]

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
        for index, spec in enumerate(self._tools.values()):
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
            # modules, web, trusted low-risk modules, then builtins. Within one
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
        if spec.module != "web" and not spec.side_effects and spec.risk == RISK_LOW:
            return 1
        if spec.module == "web" or spec.module.startswith("web:"):
            return 2
        if spec.scope == "domain":
            return 3
        if spec.scope == "global":
            return 4
        return 5

    def schemas_for_names(self, names: list[str]) -> list[dict[str, Any]]:
        """Rebuild the schemas for a persisted run snapshot.

        Unknown names are ignored: a module may have been disabled since the
        run began, and a snapshot must not resurrect it.
        """
        wanted = set(names)
        return [
            self._schema(spec)
            for spec in self._tools.values()
            if spec.name in wanted
        ]

    # -- execution --------------------------------------------------------

    def validate(
        self, call: ToolCall, *,
        allowed: set[str] | frozenset[str] | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        """Public validation used by the runtime before creating approvals."""
        spec = self.resolve(call.name, allowed=allowed, role=role)
        return validate_arguments(spec, call.arguments)

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
    ) -> dict[str, Any]:
        """Validate + dispatch + record. Never raises for tool-level problems;
        those are captured as status='failed'/'denied'. When ``execution_id``
        is given (an approval resumed), the existing record is completed in
        place instead of creating a duplicate row. ``allowed_names`` is the
        run's capability snapshot; a name outside it is denied, not run."""
        started = util.iso()
        started_monotonic = time.monotonic()
        execution_id = execution_id or util.new_id("ex")
        try:
            spec = self.resolve(
                call.name, allowed=allowed_names, role=role
            )
            arguments = validate_arguments(spec, call.arguments)
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
                "clearly worth keeping. Do not save transient chatter."
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
                            "the current domain.",
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
                        "description": "Also search archived memories. An "
                            "archived hit is restored and labelled.",
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
                },
                "required": ["name", "description", "reason"],
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
                "approval, and drafts are never activated automatically."
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
            handler=self._tool_propose_skill,
        ))

    # handlers -------------------------------------------------------------

    def _tool_remember(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        assert ctx.memory and ctx.domains
        out = ctx.memory.remember(
            args["content"],
            kind=args.get("kind"),
            scope=args.get("scope"),
            domain_id=ctx.domain_id,
            origin="agent" if ctx.task_id else "user",
            provenance={"run_id": ctx.run_id, "task_id": ctx.task_id},
            domains=ctx.domains,
            artifact_body=args.get("detail"),
            task_id=ctx.task_id,
            run_id=ctx.run_id,
        )
        mem = out["memory"]
        return {
            "memory_id": mem["id"],
            "kind": mem["kind"],
            "scope": mem["scope"],
            "saved": util.clip(mem["content"], 400),
            "duplicate": out["duplicate"],
            "superseded_id": out["superseded"]["id"] if out["superseded"] else None,
        }

    def _tool_update_memory(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        assert ctx.memory
        mem = ctx.memory.update(
            args["memory_id"], content=args["content"],
            changed_by="agent", reason=args.get("reason"),
        )
        return {"memory_id": mem["id"], "revision": mem["revision"]}

    def _tool_retract_memory(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        assert ctx.memory
        mem = ctx.memory.retract(
            args["memory_id"], changed_by="agent", reason=args["reason"]
        )
        return {"memory_id": mem["id"], "status": mem["status"]}

    def _tool_search_knowledge(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        assert ctx.memory and ctx.messages
        query = args["query"]
        priority = ["global"]
        if ctx.domain_id:
            priority.insert(0, f"domain:{ctx.domain_id}")
        memories = ctx.memory.search(
            query, scope_priority=priority,
            include_archived=bool(args.get("include_archived")),
        )
        # Explicitly returned memories count as used; this is the one bump
        # per run, deduped by the memory manager.
        ctx.memory.record_access(
            [m["id"] for m in memories], run_id=ctx.run_id
        )
        history = ctx.messages.search(query, limit=int(
            self.config.get("context.history_results", 4)
        ), exclude_task=ctx.task_id)
        return {
            "memories": [
                {
                    "id": m["id"], "kind": m["kind"], "scope": m["scope"],
                    "content": util.clip(m["content"], 500),
                    "source": "memory",
                    "archived": bool(m.get("archived")),
                    "restored": bool(m.get("restored")),
                    "access_count": m.get("access_count", 0),
                    "last_accessed_at": m.get("last_accessed_at"),
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
        ctx.tasks.set_meta(ctx.task_id, meta)
        ctx.tasks.touch(ctx.task_id)
        return {"task_id": ctx.task_id, "notes_saved": True}

    def _tool_create_domain(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        assert ctx.domains
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
        return {
            "task_id": ctx.task_id,
            "domain_id": domain["id"],
            "name": domain["name"],
            "previous_domain_id": previous_domain,
            "reason": args.get("reason") or "",
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

    def _request(self, process: subprocess.Popen, payload: dict[str, Any]) -> dict[str, Any]:
        stdin_text = util.dumps(payload) + "\n"
        process.stdin.write(stdin_text.encode("utf-8"))
        process.stdin.flush()
        # Read lines until a response with our id arrives (skip notifications).
        deadline = time.monotonic() + self.timeout_s
        lines: list[str] = []
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                raise McpError(f"MCP server '{self.name}' closed its output")
            lines.append(line.decode("utf-8", errors="replace"))
            try:
                message = json.loads(lines[-1])
            except ValueError:
                continue
            if message.get("id") == payload.get("id"):
                if "error" in message:
                    raise McpError(
                        f"MCP server '{self.name}' error: "
                        f"{message['error'].get('message')}"
                    )
                return message.get("result") or {}
        raise McpError(f"MCP server '{self.name}' timed out")

    def _launch(self) -> subprocess.Popen:
        try:
            return subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={**dict(__import__("os").environ), **(self.env or {})},
            )
        except OSError as exc:
            raise McpError(f"cannot start MCP server '{self.name}': {exc}") from exc

    def list_tools(self) -> list[dict[str, Any]]:
        process = self._launch()
        try:
            self._request(process, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "harness", "version": "2.0.0"},
                },
            })
            process.stdin.write((util.dumps({
                "jsonrpc": "2.0", "method": "notifications/initialized"
            }) + "\n").encode("utf-8"))
            process.stdin.flush()
            result = self._request(process, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            })
            return result.get("tools") or []
        finally:
            self._shutdown(process)

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        process = self._launch()
        try:
            self._request(process, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "harness", "version": "2.0.0"},
                },
            })
            process.stdin.write((util.dumps({
                "jsonrpc": "2.0", "method": "notifications/initialized"
            }) + "\n").encode("utf-8"))
            process.stdin.flush()
            result = self._request(process, {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            })
            if result.get("isError"):
                texts = [
                    item.get("text", "") for item in result.get("content") or []
                    if isinstance(item, dict)
                ]
                raise McpError("; ".join(t for t in texts if t) or "MCP tool error")
            return result
        finally:
            self._shutdown(process)

    def _shutdown(self, process: subprocess.Popen) -> None:
        try:
            process.stdin.close()
            process.wait(timeout=5)
        except Exception:
            process.kill()


def mcp_text(result: dict[str, Any]) -> str:
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "\n".join(parts)[:4000]


# MCP module loading lives in ``harness/modules/mcp.py``: it is registry-backed
# so discovery, health, domain bindings, and reload share one record shape.
# The stdio client above stays here because it is the transport, not policy.

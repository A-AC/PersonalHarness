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
        )

    def resolve(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolError(
                f"unknown tool: {name} (available: "
                f"{', '.join(sorted(self._tools))})"
            )
        return spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI function schemas for every registered tool."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    # -- execution --------------------------------------------------------

    def validate(self, call: ToolCall) -> dict[str, Any]:
        """Public validation used by the runtime before creating approvals."""
        spec = self.resolve(call.name)
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
    ) -> dict[str, Any]:
        """Validate + dispatch + record. Never raises for tool-level problems;
        those are captured as status='failed'/'denied'. When ``execution_id``
        is given (an approval resumed), the existing record is completed in
        place instead of creating a duplicate row."""
        started = util.iso()
        started_monotonic = time.monotonic()
        execution_id = execution_id or util.new_id("ex")
        try:
            spec = self.resolve(call.name)
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
                "explicit user approval; state why it is needed."
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
        memories = ctx.memory.search(query, scope_priority=priority)
        history = ctx.messages.search(query, limit=int(
            self.config.get("context.history_results", 4)
        ), exclude_task=ctx.task_id)
        return {
            "memories": [
                {
                    "id": m["id"], "kind": m["kind"], "scope": m["scope"],
                    "content": util.clip(m["content"], 500),
                    "source": "memory",
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
        if ctx.domains.by_name(name):
            return {"already_exists": name}
        domain_id = util.new_id("dom")
        domain = ctx.domains.create(
            domain_id, name=name,
            description=args.get("description", ""),
            created_by="model", seed_note=args.get("description") or None,
        )
        return {"domain_id": domain["id"], "name": domain["name"]}


# ---------------------------------------------------------------------------
# MCP: minimal stdio JSON-RPC client
# ---------------------------------------------------------------------------

class McpError(RuntimeError):
    pass


class McpServer:
    """One configured stdio MCP server. Launched per call for isolation."""

    def __init__(self, name: str, command: str, args: list[str],
                 env: dict[str, str] | None = None):
        self.name = name
        self.command = command
        self.args = args
        self.env = env

    def _request(self, process: subprocess.Popen, payload: dict[str, Any]) -> dict[str, Any]:
        stdin_text = util.dumps(payload) + "\n"
        process.stdin.write(stdin_text.encode("utf-8"))
        process.stdin.flush()
        # Read lines until a response with our id arrives (skip notifications).
        deadline = time.monotonic() + 30.0
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


class McpTools:
    """Registers tools from the configured MCP servers file."""

    def __init__(self, registry: ToolRegistry, config, events: Events):
        self.registry = registry
        self.config = config
        self.events = events
        self.servers: dict[str, McpServer] = {}
        self.status: dict[str, str] = {}

    def load(self) -> None:
        path_text = str(self.config.get("tools.mcp_servers_file", "") or "")
        if not path_text:
            return
        import yaml

        path = Path(path_text)
        if not path.is_absolute():
            path = self.registry.artifacts_dir.parent / path
        if not path.is_file():
            self.events.record(
                "mcp.config_missing", level="warn",
                data={"path": str(path)},
            )
            return
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            self.events.record(
                "mcp.config_error", level="warn", data={"error": str(exc)}
            )
            return
        for entry in data.get("servers") or []:
            name = str(entry.get("name") or "").strip()
            command = str(entry.get("command") or "").strip()
            if not name or not command:
                continue
            server = McpServer(
                name, command,
                [str(a) for a in entry.get("args") or []],
                entry.get("env") or None,
            )
            self.servers[name] = server
            self._register_server(server, entry)

    def _register_server(self, server: McpServer, entry: dict[str, Any]) -> None:
        autoapprove = bool(entry.get("autoapprove", False))
        default_risk = (
            RISK_LOW if autoapprove
            else str(self.config.get("tools.mcp_default_risk", RISK_CONFIRM))
        )
        prefix = f"mcp_{server.name}_"
        try:
            tools = server.list_tools()
        except (McpError, Exception) as exc:
            self.status[server.name] = f"unavailable: {exc}"
            self.events.record(
                "mcp.server_unavailable", level="warn",
                data={"server": server.name, "error": str(exc)},
            )
            return
        count = 0
        for tool in tools:
            tool_name = str(tool.get("name") or "").strip()
            if not tool_name:
                continue
            registered = prefix + re.sub(r"[^a-zA-Z0-9_]", "_", tool_name)
            if registered in self.registry.names():
                continue
            try:
                self.registry.register(ToolSpec(
                    name=registered,
                    description=(
                        f"[MCP {server.name}] "
                        + str(tool.get("description") or tool_name)
                    ),
                    parameters=tool.get("inputSchema")
                    or {"type": "object", "properties": {}},
                    risk=default_risk,
                    handler=self._make_handler(server, tool_name),
                ))
                count += 1
            except (ToolDenied, ToolError) as exc:
                self.events.record(
                    "mcp.tool_refused", level="warn",
                    data={"server": server.name, "tool": tool_name,
                          "error": str(exc)},
                )
        self.status[server.name] = f"ok ({count} tools)"

    def _make_handler(self, server: McpServer, tool_name: str) -> Callable:
        def handler(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
            result = server.call(tool_name, args)
            return {"content": mcp_text(result)}
        return handler

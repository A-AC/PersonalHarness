"""HTTP layer: JSON API + the web UI page. No logic of its own.

Every mutating endpoint delegates to the same App the CLI talks to over
HTTP, so the web UI and the terminal are two views of one runtime and one
state store. The UI shell is static (see ``harness/web/``) and polls the API;
there is no second source of truth in the browser.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import __version__, git_sha, util
from . import setup as setup_helpers
from .app import App
from .approvals import ApprovalError
from .config import EDITABLE_KEYS, ConfigError, save_config, validate_group_update
from .db import SCHEMA_VERSION
from .inbox import InboxError, InboxTooLarge
from .memory import KINDS as MEMORY_KINDS
from .modules.mcp import McpSetupError
from .modules.types import ModuleBinding
from .providers import ModelSelectionError, configured_provider_id
from .setup import SetupError
from .tools import ToolError

WEB_DIR = Path(__file__).resolve().parent / "web"
# Assets are served through the same authenticated handler as the page. No
# StaticFiles mount exists, so an unauthenticated browser cannot read them.
ASSETS = {
    "app.js": ("app.js", "application/javascript; charset=utf-8"),
    "style.css": ("style.css", "text/css; charset=utf-8"),
}


def _json(data: Any, status: int = 200) -> Response:
    return JSONResponse(data, status_code=status)


def _err(message: str, status: int = 400) -> Response:
    return JSONResponse({"error": message}, status_code=status)


def _domain_terms(value: Any, field: str) -> list[str] | None:
    """Normalize a domain keyword/example field from a list or string."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ValueError(f"domain {field} must be a list of strings or a string")


class Server:
    def __init__(self, app: App):
        self.app = app
        self.config = app.config

    # -- auth --------------------------------------------------------------

    def _authorized(self, request: Request) -> bool:
        token = str(self.config.get("server.token", ""))
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer ") and header[7:] == token:
            return True
        if request.query_params.get("token") == token:
            return True
        if request.cookies.get("harness_token") == token:
            return True
        return False

    def _set_cookie(self, request: Request, response: Response) -> Response:
        token = str(self.config.get("server.token", ""))
        if request.query_params.get("token") == token:
            response.set_cookie("harness_token", token, httponly=True,
                                samesite="lax")
        return response

    def _needs_setup(self) -> bool:
        return not bool(self.config.get("setup.complete"))

    async def _body(self, request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # -- basic routes --------------------------------------------------------

    async def health(self, request: Request) -> Response:
        return _json({"status": "ok"})

    async def page(self, request: Request) -> Response:
        if self._needs_setup():
            # The wizard is the only page an unconfigured instance serves; it
            # must be reachable before any token exists.
            path = WEB_DIR / "setup.html"
            if not path.is_file():
                return Response("setup page missing", status_code=500)
            response = Response(
                path.read_text(encoding="utf-8"), media_type="text/html"
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        if not self._authorized(request):
            return Response("unauthorized", status_code=401)
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        response = Response(html, media_type="text/html")
        return self._set_cookie(request, response)

    async def asset(self, request: Request) -> Response:
        if not self._authorized(request):
            return Response("unauthorized", status_code=401)
        entry = ASSETS.get(request.path_params["name"])
        if entry is None:
            return Response("not found", status_code=404)
        filename, media_type = entry
        path = WEB_DIR / filename
        if not path.is_file():
            return Response("not found", status_code=404)
        response = Response(path.read_text(encoding="utf-8"), media_type=media_type)
        response.headers["Cache-Control"] = "no-cache"
        return self._set_cookie(request, response)

    # -- first-run setup -----------------------------------------------------

    def _setup_payload(self) -> dict[str, Any]:
        payload = setup_helpers.setup_state(self.config)
        payload["templates"] = [
            {
                "id": template.get("id"),
                "title": template.get("title"),
                "description": template.get("description"),
                "fields": template.get("fields") or [],
                "installed": bool(template.get("installed")),
            }
            for template in self.app.modules.loader.templates()
        ]
        return payload

    async def setup_get(self, request: Request) -> Response:
        if not self._needs_setup():
            return _err("setup already complete", 409)
        return _json(await run_in_threadpool(self._setup_payload))

    async def setup_probe(self, request: Request) -> Response:
        if not self._needs_setup():
            return _err("setup already complete", 409)
        body = await self._body(request)
        try:
            result = await run_in_threadpool(
                setup_helpers.probe_model_endpoint,
                body.get("base_url"),
                api_key=str(body.get("api_key") or ""),
                model=str(body.get("model") or ""),
            )
        except SetupError as exc:
            return _err(str(exc), exc.status)
        return _json(result)

    async def setup_submit(self, request: Request) -> Response:
        if not self._needs_setup():
            return _err("setup already complete", 409)
        body = await self._body(request)
        modules = body.get("modules")
        if modules is not None and not isinstance(modules, list):
            return _err("modules must be a list")
        values = {
            "token": body.get("token"),
            "model_base_url": body.get("model_base_url"),
            "model_name": body.get("model_name"),
            "api_key": body.get("api_key") or "",
            "timezone": body.get("timezone") or "system",
        }
        try:
            normalized = await run_in_threadpool(
                setup_helpers.apply_setup, self.config, values
            )
        except SetupError as exc:
            return _err(str(exc), exc.status)
        except OSError as exc:
            return _err(f"could not save configuration: {exc}", 500)

        results: list[dict[str, Any]] = []
        installed: list[str] = []
        for item in modules or []:
            if not isinstance(item, dict):
                results.append({
                    "id": "", "installed": False,
                    "error": "module entry must be an object",
                })
                continue
            module_id = str(item.get("id") or "")
            try:
                await run_in_threadpool(
                    self.app.modules.loader.install_template,
                    module_id,
                    item.get("values") or {},
                    confirm=True,
                )
            except Exception as exc:
                # One broken optional module must not fail the whole setup.
                results.append({
                    "id": module_id,
                    "installed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            else:
                results.append({
                    "id": module_id, "installed": True, "error": "",
                })
                installed.append(module_id)

        self.app.events.record(
            "system.configured",
            data={"source": "wizard", "modules": installed},
        )
        response = _json({
            "status": "configured",
            "modules": results,
            "env_overrides": setup_helpers.setup_state(
                self.config
            )["env_overrides"],
        })
        response.set_cookie(
            "harness_token", normalized["token"],
            httponly=True, samesite="lax",
        )
        return response

    # -- state --------------------------------------------------------------

    async def state(self, request: Request) -> Response:
        """Compatibility aggregate (CLI status/tasks use it).

        The browser uses the small ``/api/summary`` plus one panel endpoint at
        a time, but this endpoint remains the single-shot view for scripts and
        the CLI.
        """
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json(await run_in_threadpool(self._state_payload))

    # -- panel payloads -----------------------------------------------------

    def _visible_tasks(
        self, include_subagents: bool = False
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Open work first, then recent history.

        Children stay off the default list; ``include_subagents`` exposes the
        tree as flat rows so delegated work can be inspected from the UI.
        """
        app = self.app

        def visible(task: dict[str, Any]) -> bool:
            return include_subagents or (
                (task.get("role") or "user") != "subagent"
            )

        open_tasks = [
            task for task in app.tasks.list_open() if visible(task)
        ]
        seen = {task["id"] for task in open_tasks}
        tasks = open_tasks + [
            task for task in app.tasks.list_recent(limit=60)
            if task["id"] not in seen and visible(task)
        ]
        return open_tasks, tasks

    def _tasks_payload(
        self, include_subagents: bool = False
    ) -> dict[str, Any]:
        open_tasks, tasks = self._visible_tasks(include_subagents)
        return {
            "tasks": tasks,
            "open_tasks": open_tasks,
            "counts": {
                "open_tasks": len(open_tasks),
                "paused": len(
                    [task for task in tasks if task["status"] == "paused"]
                ),
                "subagents": len(
                    [task for task in tasks
                     if (task.get("role") or "user") == "subagent"]
                ),
            },
        }

    def _approval_payload(self, approval: dict[str, Any]) -> dict[str, Any]:
        """Add the fields needed to render and edit an approval."""
        spec = self.app.tools.resolve_optional(
            str(approval.get("tool") or "")
        )
        action = (approval.get("meta") or {}).get("action", "tool")
        meta = approval.get("meta") or {}
        feedback = meta.get("feedback") if isinstance(meta, dict) else ""
        approval["summary"] = self.app.runtime.summary_for(approval)
        approval["schema"] = (
            dict(spec.parameters) if spec is not None else {}
        )
        approval["editable"] = bool(
            action in ("tool", "create_domain") and spec is not None
        )
        approval["supersedes_id"] = approval.get("supersedes_id")
        approval["feedback"] = str(feedback or "")
        approval["side_effects"] = bool(spec.side_effects) if spec else True
        approval["read_only"] = bool(spec.read_only) if spec else False
        approval["tainted"] = bool(
            self.app.runtime.run_tainted(approval["run_id"])
        )
        return approval

    def _approvals_payload(self) -> dict[str, Any]:
        approvals = [
            self._approval_payload(approval)
            for approval in self.app.approvals.pending()
        ]
        return {
            "approvals": approvals,
            "counts": {"pending_approvals": len(approvals)},
        }

    def _memory_counts(self) -> dict[str, int]:
        app = self.app

        def count(where: str) -> int:
            row = app.db.query_one(
                f"SELECT COUNT(*) AS n FROM memories WHERE {where}"
            )
            return int(row["n"] if row else 0)

        return {
            "active": count("status='active' AND archived_at IS NULL"),
            "archived": count("status='active' AND archived_at IS NOT NULL"),
            "superseded": count("status='superseded'"),
            "retracted": count("status='retracted'"),
            "expired": count("status='expired'"),
            "pinned": count(
                "status='active' AND archived_at IS NULL AND pinned=1"
            ),
        }

    def _count_inbox_new(self) -> int:
        row = self.app.db.query_one(
            "SELECT COUNT(*) AS n FROM inbox_events WHERE status='new'"
        )
        return int(row["n"] if row else 0)

    def _scheduler_payload(self) -> dict[str, Any]:
        app = self.app
        report = util.loads(
            app.db.meta_get("scheduler.last_report") or "", {}
        )
        return {
            "last_tick": app.db.meta_get("scheduler.last_tick"),
            "last_report": report,
            "enabled": bool(app.config.get("scheduler.enabled", True)),
        }

    def _summary_payload(self) -> dict[str, Any]:
        """Small header contract: badges plus the task the agent is on."""
        app = self.app
        open_tasks, tasks = self._visible_tasks()
        approvals = app.approvals.pending()
        domains = app.domains.list_all()
        memory_counts = self._memory_counts()
        modules = app.modules.all()
        working = None
        if open_tasks:
            task = open_tasks[0]
            run = app.db.query_one(
                "SELECT status, decision, iterations, tool_calls FROM runs"
                " WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
                (task["id"],),
            )
            task_meta = util.loads(task.get("meta") or "{}", {})
            routing = task_meta.get("routing") if isinstance(task_meta, dict) else None
            working = {
                "id": task["id"],
                "objective": task.get("objective"),
                "status": task.get("status"),
                "status_reason": task.get("status_reason"),
                "domain_id": task.get("domain_id"),
                "origin": task.get("origin"),
                "updated_at": task.get("updated_at"),
                "run_status": run["status"] if run else None,
                "decision": run["decision"] if run else None,
                "iterations": int(run["iterations"]) if run else 0,
                "tool_calls": int(run["tool_calls"]) if run else 0,
                "routing": routing if isinstance(routing, dict) else None,
            }
        return {
            "counts": {
                "open_tasks": len(open_tasks),
                "paused": len(
                    [task for task in tasks if task["status"] == "paused"]
                ),
                "pending_approvals": len(approvals),
                "domains": len(domains),
                "inbox_new": self._count_inbox_new(),
                "memories": memory_counts["active"],
                "archived_memories": memory_counts["archived"],
                "skills": len([m for m in modules if m.kind == "skill"]),
                "modules": len([m for m in modules if m.kind != "skill"]),
            },
            "working_task": working,
            "model": {
                "provider": configured_provider_id(app.config),
                "name": app.config.get("model.name"),
                "context_window": int(
                    app.config.get("model.context_window", 0) or 0
                ),
            },
            "persona": app.config.get("persona", {}),
            "domains": [
                {"id": d["id"], "name": d["name"], "status": d["status"]}
                for d in domains
            ],
            "scheduler": self._scheduler_payload(),
            "now": util.iso(),
            "system_timezone": util.system_tz_name(),
        }

    def _state_payload(self) -> dict[str, Any]:
        app = self.app
        summary = self._summary_payload()
        tasks = self._tasks_payload()
        approvals = self._approvals_payload()
        memories_all = app.memory.list(status="any", limit=100)
        memory_counts = self._memory_counts()
        inbox_events = app.inbox.list_events(limit=60)
        automations = app.inbox.list_automations()
        automation_fires = app.inbox.list_fires(limit=40)
        return {
            **summary,
            **tasks,
            "messages": app.messages.list_visible(limit=300),
            **approvals,
            "schedules": app.scheduler.list_schedules(),
            "memories": [
                m for m in memories_all
                if not m.get("archived") and m.get("status") == "active"
            ],
            "memories_archived": [
                m for m in memories_all if m.get("archived")
            ],
            "memory_counts": memory_counts,
            "tools": [
                {"name": spec.name, "risk": spec.risk,
                 "module": spec.module,
                 "description": spec.description[:160]}
                for spec in app.tools.specs()
            ],
            "events": app.events.list(limit=120),
            "config": app.config.redacted(),
            "consolidations": app.consolidation.list_runs(limit=5),
            "inbox_events": inbox_events,
            "automations": automations,
            "automation_fires": automation_fires,
            "counts": {
                **summary["counts"],
                "inbox_events": len(inbox_events),
                "automations": len(automations),
            },
        }

    async def summary(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json(await run_in_threadpool(self._summary_payload))

    async def messages(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        try:
            limit = int(request.query_params.get("limit", 300))
        except ValueError:
            return _err("limit must be an integer")
        limit = max(1, min(limit, 1000))
        return _json({
            "messages": self.app.messages.list_visible(limit=limit),
        })

    async def tasks_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        include = request.query_params.get("include_subagents", "").lower()
        return _json(await run_in_threadpool(
            self._tasks_payload, include in ("1", "true", "yes")
        ))

    async def approvals_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json(await run_in_threadpool(self._approvals_payload))

    async def events(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        limit = int(request.query_params.get("limit", 200))
        return _json({"events": self.app.events.list(limit=limit)})

    # -- conversation ---------------------------------------------------------

    async def message(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        text = str(body.get("text", "")).strip()
        if not text:
            return _err("empty message")
        source = str(body.get("source", "web"))
        domain_id = body.get("domain_id") or None
        if domain_id and self.app.domains.get(str(domain_id)) is None:
            return _err("no such domain", 404)
        try:
            result = await run_in_threadpool(
                self.app.runtime.submit_user_message, text, source=source,
                domain_id=domain_id,
            )
        except Exception as exc:
            return _err(f"{type(exc).__name__}: {exc}", 500)
        return _json({
            "status": result.status,
            "run_id": result.run_id,
            "task_id": result.task_id,
            "message": result.message,
            "approval_id": result.approval_id,
        })

    # -- tasks ----------------------------------------------------------------

    async def task_detail(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        task_id = request.path_params["task_id"]
        task = self.app.tasks.get(task_id)
        if task is None:
            return _err("no such task", 404)
        runs = self.app.db.query(
            "SELECT id, status, trigger, trigger_ref, started_at, ended_at,"
            " iterations, tool_calls, decision, error, parent_run_id,"
            " transcript, context_sources, meta FROM runs WHERE task_id=?"
            " ORDER BY started_at",
            (task_id,),
        )
        runs_payload: list[dict[str, Any]] = []
        latest_toolset = None
        for row in runs:
            item = dict(row)
            meta = util.loads(item.pop("meta", None) or "{}", {})
            item["toolset"] = meta.get("toolset")
            item["tainted_sources"] = meta.get("tainted_sources", [])
            item["model"] = meta.get("model")
            item["provider"] = meta.get("provider")
            # The full model request is the persisted transcript: system
            # prompt, user turn, assistant/tool turns, and tool results. The
            # UI shows it verbatim instead of an abstracted summary.
            item["transcript"] = util.loads(
                item.pop("transcript", None) or "[]", []
            )
            item["context_sources"] = util.loads(
                item.pop("context_sources", None) or "[]", []
            )
            if item["toolset"]:
                latest_toolset = item["toolset"]
            runs_payload.append(item)
        executions = self.app.db.query(
            "SELECT * FROM tool_executions WHERE task_id=? ORDER BY"
            " started_at",
            (task_id,),
        )
        task_meta = util.loads(task.get("meta") or "{}", {})
        routing = task_meta.get("routing") if isinstance(task_meta, dict) else None
        entered_row = self.app.db.query_one(
            "SELECT ts, data FROM event_log WHERE type='domain.entered'"
            " AND task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        domain_entered = None
        if entered_row is not None:
            entered_data = util.loads(entered_row["data"] or "{}", {})
            domain_entered = {
                "data": entered_data if isinstance(entered_data, dict) else {},
                "ts": entered_row["ts"],
            }
        children = []
        for child in self.app.tasks.children(task_id):
            meta = util.loads(child.get("meta") or "{}", {})
            payload = dict(child)
            payload["result"] = meta.get("result")
            payload["result_path"] = meta.get("result_path")
            payload["blocked_reason"] = meta.get("blocked_reason")
            payload["goal_hash"] = meta.get("goal_hash")
            children.append(payload)
        return _json({
            "task": task,
            "children": children,
            "domain": self.app.domains.get(task.get("domain_id"))
            if task.get("domain_id") else None,
            "routing": routing if isinstance(routing, dict) else None,
            "domain_entered": domain_entered,
            "runs": runs_payload,
            "toolset": latest_toolset,
            "executions": [dict(r) for r in executions],
            "approvals": [
                self._approval_payload(a)
                for a in self.app.approvals.pending(task_id)
            ],
            "messages": self.app.messages.for_task(task_id),
            "context_sources": util.loads(
                (self.app.db.query_one(
                    "SELECT context_sources FROM runs WHERE task_id=?"
                    " ORDER BY started_at DESC LIMIT 1", (task_id,)
                ) or {"context_sources": "[]"})["context_sources"],
                [],
            ),
        })

    async def task_action(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        task_id = request.path_params["task_id"]
        action = request.path_params["action"]
        task = self.app.tasks.get(task_id)
        if task is None:
            return _err("no such task", 404)
        body = await self._body(request)
        if action == "cancel":
            try:
                result = await run_in_threadpool(
                    self.app.runtime.cancel_task, task_id
                )
            except ValueError as exc:
                return _err(str(exc), 404)
            return _json({"task": self.app.tasks.get(task_id),
                          "status": result.status, "message": result.message})
        if action == "domain":
            domain_id = body.get("domain_id")
            if not isinstance(domain_id, str) or not domain_id.strip():
                return _err("domain_id is required", 400)
            domain_id = domain_id.strip()
            reason = body.get("reason", "")
            if not isinstance(reason, str):
                return _err("reason must be a string", 400)
            if len(reason) > 1000:
                return _err("reason is too long", 400)
            domain = self.app.domains.get(domain_id)
            if domain is None:
                return _err("no such domain", 404)
            if domain.get("status") != "active":
                return _err("domain is archived", 400)
            try:
                corrected = await run_in_threadpool(
                    self.app.runtime.correct_task_domain,
                    task_id, domain_id,
                    reason=reason, source="web",
                )
            except KeyError:
                return _err("no such task or domain", 404)
            except ValueError as exc:
                return _err(str(exc), 400)
            return _json(corrected)
        if action == "resume":
            try:
                result = await run_in_threadpool(
                    self.app.runtime.retry_paused_task, task
                )
            except Exception as exc:
                return _err(f"{type(exc).__name__}: {exc}", 500)
            return _json({"status": result.status, "task_id": result.task_id,
                          "message": result.message})
        return _err("invalid action")

    # -- approvals --------------------------------------------------------------

    async def approval_decide(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        approval_id = request.path_params["approval_id"]
        decision = request.path_params["decision"]
        if decision not in (
            "approve", "reject", "cancel", "revise", "edit"
        ):
            return _err("invalid decision", 400)
        approval = self.app.approvals.get(approval_id)
        if approval is None:
            return _err("no such approval", 404)
        body = await self._body(request)
        feedback = body.get("feedback", "")
        if not isinstance(feedback, str):
            return _err("feedback must be a string", 400)
        arguments = body.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            return _err("arguments must be a JSON object", 400)
        remember = body.get("remember", False)
        if not isinstance(remember, bool):
            return _err("remember must be a boolean", 400)
        try:
            result = await run_in_threadpool(
                self.app.runtime.decide_approval,
                approval_id, decision, "web",
                feedback=feedback,
                arguments=arguments,
                remember=remember,
            )
        except ApprovalError as exc:
            return _err(str(exc), 409)
        except ValueError as exc:
            return _err(str(exc), 400)
        payload = {
            "status": result.status, "message": result.message,
            "task_id": result.task_id, "approval_id": result.approval_id,
        }
        latest_id = result.approval_id or approval_id
        latest = self.app.approvals.get(latest_id)
        if latest is not None:
            payload["summary"] = self.app.runtime.summary_for(latest)
        return _json(payload)

    # -- schedules ----------------------------------------------------------------

    async def schedules_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json({"schedules": self.app.scheduler.list_schedules()})

    async def schedules_create(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        try:
            schedule = self.app.scheduler.create_schedule(
                name=str(body.get("name", "")),
                prompt=str(body.get("prompt", "")),
                kind=str(body.get("kind", "")),
                tz=body.get("tz"),
                at_ts=body.get("at_ts"),
                interval_s=body.get("interval_s"),
                time_of_day=body.get("time_of_day"),
                weekday=body.get("weekday"),
                domain_id=body.get("domain_id") or None,
            )
        except ValueError as exc:
            return _err(str(exc))
        return _json({"schedule": schedule})

    async def schedule_action(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        schedule_id = request.path_params["schedule_id"]
        action = request.path_params["action"]
        if action == "enable":
            return _json({"schedule": self.app.scheduler.set_enabled(schedule_id, True)})
        if action == "disable":
            return _json({"schedule": self.app.scheduler.set_enabled(schedule_id, False)})
        if action == "delete":
            self.app.scheduler.delete_schedule(schedule_id)
            return _json({"deleted": schedule_id})
        return _err("invalid action")

    # -- inbox / automations -----------------------------------------------------

    @staticmethod
    def _inbox_status(exc: InboxError) -> int:
        return int(getattr(exc, "status_code", 400) or 400)

    async def inbox_receive(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        # Bound the request before parsing JSON. Body content is checked again
        # by the manager so a missing Content-Length cannot bypass the limit.
        try:
            length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            length = 0
        max_body = max(1, int(self.app.config.get(
            "inbox.max_body_chars", 12000
        )))
        if length > max_body * 2 + 20000:
            return _err(
                f"request exceeds inbox.max_body_chars ({max_body})", 413
            )
        body = await self._body(request)
        try:
            outcome = await run_in_threadpool(
                self.app.inbox.receive,
                source=body.get("source"),
                external_id=body.get("external_id"),
                title=body.get("title"),
                body=body.get("body", ""),
                kind=body.get("kind", "event"),
                severity=body.get("severity", "info"),
                meta=body.get("meta"),
            )
        except InboxTooLarge as exc:
            return _err(str(exc), 413)
        except InboxError as exc:
            return _err(str(exc), self._inbox_status(exc))
        except Exception as exc:
            return _err(f"{type(exc).__name__}: {exc}", 500)
        event = await run_in_threadpool(
            self.app.inbox.get_event, outcome["id"]
        )
        return _json({**outcome, "event": event})

    async def inbox_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        status = request.query_params.get("status") or None
        try:
            limit = int(request.query_params.get("limit", 100))
        except ValueError:
            return _err("limit must be an integer")
        try:
            events = await run_in_threadpool(
                self.app.inbox.list_events, status, limit
            )
        except InboxError as exc:
            return _err(str(exc), self._inbox_status(exc))
        automations = await run_in_threadpool(
            self.app.inbox.list_automations
        )
        fires = await run_in_threadpool(
            self.app.inbox.list_fires, 80
        )
        return _json({
            "events": events,
            "automations": automations,
            "fires": fires,
            "counts": {
                "new": len([e for e in events if e["status"] == "new"]),
            },
        })

    async def inbox_ignore(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        event_id = request.path_params["event_id"]
        body = await self._body(request)
        try:
            event = await run_in_threadpool(
                self.app.inbox.ignore, event_id,
                reason=body.get("reason"), by="web",
            )
        except KeyError as exc:
            return _err(str(exc), 404)
        except InboxError as exc:
            return _err(str(exc), self._inbox_status(exc))
        return _json({"event": event})

    async def inbox_triage(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        report = await run_in_threadpool(self.app.inbox.triage)
        return _json({"report": report})

    async def automations_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        automations = await run_in_threadpool(
            self.app.inbox.list_automations
        )
        fires = await run_in_threadpool(self.app.inbox.list_fires, 80)
        return _json({"automations": automations, "fires": fires})

    async def automations_create(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        try:
            automation = await run_in_threadpool(
                self.app.inbox.create_automation,
                name=body.get("name"),
                enabled=bool(body.get("enabled", True)),
                source=body.get("source"),
                kind=body.get("kind"),
                match_keywords=body.get("match_keywords"),
                match_regex=body.get("match_regex"),
                action=body.get("action"),
                prompt_template=body.get("prompt_template", ""),
                domain_id=body.get("domain_id"),
                cooldown_s=body.get("cooldown_s", 0),
                max_per_hour=body.get("max_per_hour"),
                meta=body.get("meta"),
            )
        except InboxTooLarge as exc:
            return _err(str(exc), 413)
        except InboxError as exc:
            return _err(str(exc), self._inbox_status(exc))
        return _json({"automation": automation})

    async def automation_update(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        automation_id = request.path_params["automation_id"]
        body = await self._body(request)
        try:
            automation = await run_in_threadpool(
                self.app.inbox.update_automation,
                automation_id, **body,
            )
        except KeyError as exc:
            return _err(str(exc), 404)
        except InboxTooLarge as exc:
            return _err(str(exc), 413)
        except InboxError as exc:
            return _err(str(exc), self._inbox_status(exc))
        except (TypeError, ValueError) as exc:
            return _err(str(exc), 400)
        return _json({"automation": automation})

    async def automation_action(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        automation_id = request.path_params["automation_id"]
        action = request.path_params["action"]
        automation = self.app.inbox.get_automation(automation_id)
        if automation is None:
            return _err("no such automation", 404)
        try:
            if action == "enable":
                updated = await run_in_threadpool(
                    self.app.inbox.set_automation_enabled,
                    automation_id, True,
                )
                return _json({"automation": updated})
            if action == "disable":
                updated = await run_in_threadpool(
                    self.app.inbox.set_automation_enabled,
                    automation_id, False,
                )
                return _json({"automation": updated})
            if action == "delete":
                await run_in_threadpool(
                    self.app.inbox.delete_automation, automation_id
                )
                return _json({"deleted": automation_id})
        except KeyError as exc:
            return _err(str(exc), 404)
        return _err("invalid action")

    # -- memories ---------------------------------------------------------------

    async def memories_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        scope = request.query_params.get("scope")
        status = request.query_params.get("status", "active")
        pinned_raw = request.query_params.get("pinned")
        pinned: bool | None = None
        if pinned_raw not in (None, ""):
            lowered = pinned_raw.strip().lower()
            if lowered in ("1", "true", "yes"):
                pinned = True
            elif lowered in ("0", "false", "no"):
                pinned = False
            else:
                return _err("pinned must be 1, 0, true, or false", 400)
        try:
            limit = int(request.query_params.get("limit", 200))
        except ValueError:
            return _err("limit must be an integer")
        limit = max(1, min(limit, 1000))
        payload = await run_in_threadpool(
            self._memories_payload, scope, status, limit, pinned
        )
        return _json(payload)

    def _memories_payload(
        self, scope: str | None, status: str, limit: int = 200,
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "memories": self.app.memory.list(
                scope=scope, status=status, limit=limit, pinned=pinned
            ),
            "artifact_problems": self.app.memory.check_artifacts(),
            "counts": self._memory_counts(),
            "filter": status,
            "kinds": list(MEMORY_KINDS),
        }

    async def memory_detail(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        memory_id = request.path_params["memory_id"]
        memory = await run_in_threadpool(self.app.memory.get, memory_id)
        if memory is None:
            return _err("no such memory", 404)
        revisions = await run_in_threadpool(
            self.app.memory.revisions, memory_id
        )
        return _json({"memory": memory, "revisions": revisions})

    async def memory_search(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        query = str(body.get("query", ""))
        priority = ["global"]
        domain_id = body.get("domain_id")
        if domain_id:
            priority.insert(0, f"domain:{domain_id}")
        results = await run_in_threadpool(
            self.app.memory.search, query, scope_priority=priority,
            include_archived=bool(body.get("include_archived")),
        )
        # A search from the UI is an explicit read; count what it returned.
        await run_in_threadpool(
            self.app.memory.record_access,
            [item["id"] for item in results], None,
        )
        # Refresh the returned rows so usage fields reflect this read.
        for item in results:
            fresh = await run_in_threadpool(
                self.app.memory.get, item["id"]
            )
            if fresh is not None:
                item["access_count"] = fresh.get("access_count", 0)
                item["last_accessed_at"] = fresh.get("last_accessed_at")
        history = await run_in_threadpool(
            self.app.messages.search, query,
            limit=int(self.app.config.get("context.history_results", 4)),
        )
        return _json({"results": results, "history": history})

    async def memory_update(self, request: Request) -> Response:
        """Edit a live memory's text or kind from the UI."""
        if not self._authorized(request):
            return _err("unauthorized", 401)
        memory_id = request.path_params["memory_id"]
        if self.app.memory.get(memory_id) is None:
            return _err("no such memory", 404)
        body = await self._body(request)
        content = body.get("content")
        kind = body.get("kind")
        if content is None and kind is None:
            return _err("content or kind is required")
        try:
            updated = await run_in_threadpool(
                self.app.memory.update,
                memory_id,
                content=(str(content) if content is not None else None),
                kind=(str(kind) if kind is not None else None),
                changed_by="web",
                reason=str(body.get("reason") or "edited in the web UI"),
            )
        except ValueError as exc:
            return _err(str(exc), 409)
        return _json({"memory": updated})

    async def memory_delete(self, request: Request) -> Response:
        """Retract a memory. Retraction keeps its revision history."""
        if not self._authorized(request):
            return _err("unauthorized", 401)
        memory_id = request.path_params["memory_id"]
        if self.app.memory.get(memory_id) is None:
            return _err("no such memory", 404)
        body = await self._body(request)
        try:
            updated = await run_in_threadpool(
                self.app.memory.retract,
                memory_id,
                changed_by="web",
                reason=str(body.get("reason") or "deleted in the web UI"),
            )
        except ValueError as exc:
            return _err(str(exc), 409)
        return _json({"memory": updated})

    async def memory_action(self, request: Request) -> Response:
        """Archive, restore, pin, or unpin one memory."""
        if not self._authorized(request):
            return _err("unauthorized", 401)
        memory_id = request.path_params["memory_id"]
        action = request.path_params["action"]
        memory = self.app.memory.get(memory_id)
        if memory is None:
            return _err("no such memory", 404)
        try:
            if action == "archive":
                updated = await run_in_threadpool(
                    self.app.memory.archive, memory_id, changed_by="web",
                )
            elif action == "restore":
                updated = await run_in_threadpool(
                    self.app.memory.restore, memory_id, changed_by="web",
                )
            elif action in ("pin", "unpin"):
                updated = await run_in_threadpool(
                    self.app.memory.set_pinned, memory_id,
                    action == "pin", changed_by="web",
                )
            else:
                return _err("invalid action")
        except ValueError as exc:
            return _err(str(exc), 409)
        return _json({"memory": updated})

    # -- domains ------------------------------------------------------------------

    async def domains_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json(await run_in_threadpool(self._domains_payload))

    def _domain_suggestions_payload(
        self, status: str = "open", limit: int = 100
    ) -> dict[str, Any]:
        """Serialize suggestions with a bounded view of their task context."""
        rows = self.app.domain_suggestions.list(status=status, limit=limit)
        suggestions: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            task_ids = list(item.get("task_ids") or [])
            context_ids = task_ids[:20]
            tasks_by_id: dict[str, dict[str, Any]] = {}
            if context_ids:
                marks = ",".join("?" for _ in context_ids)
                for task_row in self.app.db.query(
                    f"SELECT id, objective FROM tasks WHERE id IN ({marks})",
                    tuple(context_ids),
                ):
                    tasks_by_id[str(task_row["id"])] = {
                        "id": str(task_row["id"]),
                        "objective": util.clip(
                            str(task_row["objective"] or ""), 300
                        ),
                    }
            tasks = [
                tasks_by_id[task_id] for task_id in context_ids
                if task_id in tasks_by_id
            ]
            objectives = [task["objective"] for task in tasks]
            item.update({
                "tasks": tasks,
                "objectives": objectives,
                "task_objectives": objectives,
            })
            suggestions.append(item)
        return {"suggestions": suggestions, "count": len(suggestions)}

    async def domain_suggestions_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        status = request.query_params.get("status", "open")
        if status not in ("open", "created", "dismissed", "all"):
            return _err("unknown suggestion status", 400)
        try:
            limit = int(request.query_params.get("limit", 100))
        except ValueError:
            return _err("limit must be an integer", 400)
        limit = max(1, min(limit, 1000))
        try:
            payload = await run_in_threadpool(
                self._domain_suggestions_payload, status, limit
            )
        except ValueError as exc:
            return _err(str(exc), 400)
        return _json(payload)

    async def domain_suggestion_dismiss(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        suggestion_id = request.path_params["suggestion_id"]
        try:
            suggestion = await run_in_threadpool(
                self.app.domain_suggestions.dismiss, suggestion_id
            )
        except ValueError as exc:
            return _err(str(exc), 404)
        payload = self._domain_suggestions_payload(status="all", limit=1000)
        enriched = next(
            (
                item for item in payload["suggestions"]
                if item.get("id") == suggestion.get("id")
            ),
            suggestion,
        )
        return _json({"suggestion": enriched})

    def _domains_payload(self) -> dict[str, Any]:
        """Environment cards for every domain, with bounded grouped counts."""
        app = self.app
        domains = app.domains.list_all()

        memory_counts = {
            str(row["scope"]): int(row["n"])
            for row in app.db.query(
                "SELECT scope, COUNT(*) AS n FROM memories"
                " WHERE status='active' AND archived_at IS NULL"
                " AND scope LIKE 'domain:%' GROUP BY scope"
            )
        }
        skill_counts = {
            str(row["domain_id"]): int(row["n"])
            for row in app.db.query(
                "SELECT b.domain_id, COUNT(*) AS n FROM module_bindings b"
                " JOIN modules m ON m.id=b.module_id"
                " WHERE b.enabled=1 AND m.kind='skill' AND m.status='active'"
                " GROUP BY b.domain_id"
            )
        }
        module_counts = {
            str(row["domain_id"]): int(row["n"])
            for row in app.db.query(
                "SELECT b.domain_id, COUNT(*) AS n FROM module_bindings b"
                " JOIN modules m ON m.id=b.module_id"
                " WHERE b.enabled=1 AND m.kind='mcp' AND m.status='active'"
                " GROUP BY b.domain_id"
            )
        }
        task_stats = {
            str(row["domain_id"]): (int(row["n"]), row["last_used"])
            for row in app.db.query(
                "SELECT domain_id, COUNT(*) AS n,"
                " MAX(last_activity_at) AS last_used FROM tasks"
                " WHERE domain_id IS NOT NULL GROUP BY domain_id"
            )
        }
        routed_counts = {
            str(row["domain_id"]): int(row["n"])
            for row in app.db.query(
                "SELECT domain_id, COUNT(*) AS n FROM event_log"
                " WHERE type='domain.routed' AND domain_id IS NOT NULL"
                " GROUP BY domain_id"
            )
        }
        corrected_away = {
            str(row["routed_domain"]): int(row["n"])
            for row in app.db.query(
                "SELECT routed_domain, COUNT(*) AS n FROM routing_feedback"
                " WHERE routed_domain IS NOT NULL GROUP BY routed_domain"
            )
        }
        corrected_in = {
            str(row["correct_domain"]): int(row["n"])
            for row in app.db.query(
                "SELECT correct_domain, COUNT(*) AS n FROM routing_feedback"
                " WHERE correct_domain IS NOT NULL GROUP BY correct_domain"
            )
        }

        cards: list[dict[str, Any]] = []
        for domain in domains:
            domain_id = str(domain["id"])
            task_count, last_used = task_stats.get(domain_id, (0, None))
            cards.append({
                **domain,
                "counts": {
                    "memories": memory_counts.get(
                        f"domain:{domain_id}", 0
                    ),
                    "skills": skill_counts.get(domain_id, 0),
                    "modules": module_counts.get(domain_id, 0),
                    "tasks": task_count,
                    "last_used": last_used,
                },
                "routing_stats": {
                    "routed_here": routed_counts.get(domain_id, 0),
                    "corrected_away": corrected_away.get(domain_id, 0),
                    "corrected_in": corrected_in.get(domain_id, 0),
                },
            })
        suggestions = self._domain_suggestions_payload(
            status="open", limit=100
        )["suggestions"]
        return {"domains": cards, "suggestions": suggestions}

    async def domains_feedback(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        limit: int | None = None
        raw_limit = request.query_params.get("limit")
        if raw_limit not in (None, ""):
            try:
                limit = int(raw_limit)
            except ValueError:
                return _err("limit must be an integer", 400)
            limit = max(1, min(limit, 1000))
        scenarios = await run_in_threadpool(
            self.app.routing_feedback.export_scenarios, limit
        )
        return _json({"scenarios": scenarios, "count": len(scenarios)})

    async def domains_route_preview(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return _err("text is required", 400)
        text = text.strip()
        if len(text) > 4000:
            return _err("text is too long", 400)
        routing = await run_in_threadpool(
            self.app.runtime.preview_route, text
        )
        return _json({"routing": routing})

    async def domains_create(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        suggestion_id = body.get("suggestion_id")
        if suggestion_id is not None and (
            not isinstance(suggestion_id, str) or not suggestion_id.strip()
        ):
            return _err("suggestion_id must be a non-empty string", 400)
        suggestion_id = suggestion_id.strip() if suggestion_id else None
        name = str(body.get("name", "")).strip()
        if not name:
            return _err("name required")
        if self.app.domains.by_name(name):
            return _err("a domain with that name exists", 409)
        try:
            keywords = _domain_terms(body.get("keywords"), "keywords")
            examples = _domain_terms(body.get("examples"), "examples")
            instructions = body.get("instructions", "")
            if not isinstance(instructions, str):
                raise ValueError("domain instructions must be a string")
            summary = body.get("summary", "")
            if not isinstance(summary, str):
                raise ValueError("domain summary must be a string")
            domain_id = util.new_id("dom")
            domain = self.app.domains.create(
                domain_id, name=name,
                description=str(body.get("description", "")),
                created_by="web", seed_note=body.get("note") or None,
                keywords=keywords or [],
                examples=examples or [],
                instructions=instructions,
                summary=summary,
            )
        except ValueError as exc:
            return _err(str(exc), 400)
        if suggestion_id:
            # A stale/unknown suggestion id must not prevent the explicit
            # domain creation. Known suggestions are closed as ``created``.
            if self.app.domain_suggestions.get(suggestion_id) is not None:
                self.app.domain_suggestions.mark_created(suggestion_id)
        return _json({"domain": domain})

    async def domain_update(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        domain_id = request.path_params["domain_id"]
        if self.app.domains.get(domain_id) is None:
            return _err("no such domain", 404)
        body = await self._body(request)
        name = body.get("name")
        if name is not None:
            name = str(name).strip()
            if not name:
                return _err("name cannot be empty")
            existing = self.app.domains.by_name(name)
            if existing and existing["id"] != domain_id:
                return _err("a domain with that name exists", 409)
        try:
            keywords = _domain_terms(body.get("keywords"), "keywords")
            examples = _domain_terms(body.get("examples"), "examples")
            instructions = body.get("instructions")
            if instructions is not None and not isinstance(instructions, str):
                raise ValueError("domain instructions must be a string")
            summary = body.get("summary")
            if summary is not None and not isinstance(summary, str):
                raise ValueError("domain summary must be a string")
            domain = self.app.domains.update(
                domain_id,
                name=name,
                description=(
                    str(body["description"])
                    if "description" in body else None
                ),
                status=body.get("status"),
                keywords=keywords,
                examples=examples,
                instructions=instructions,
                summary=summary,
            )
        except (KeyError, ValueError) as exc:
            return _err(str(exc), 400)
        return _json({"domain": domain})

    async def heartbeat(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        report = await run_in_threadpool(self.app.scheduler.heartbeat)
        return _json({"report": report})

    # -- consolidation -----------------------------------------------------------

    async def consolidate(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        report = await run_in_threadpool(
            self.app.consolidation.run, trigger="manual"
        )
        return _json(report)

    # -- modules / tools / endpoint diagnostics -----------------------------------

    async def modules_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json(await run_in_threadpool(self._modules_payload))

    async def module_create(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        try:
            module = await run_in_threadpool(
                self.app.modules.loader.create_server,
                body,
                confirm=body.get("confirm") is True,
                allow_any_command=body.get("allow_any_command") is True,
            )
        except McpSetupError as exc:
            return _err(str(exc), exc.status)
        except ValueError as exc:
            return _err(str(exc), 400)
        return _json({"module": self._module_payload(module)})

    async def module_test(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        preview = await run_in_threadpool(
            self.app.modules.loader.test_server,
            body,
            module_id=(
                str(body.get("module_id")) if body.get("module_id") else None
            ),
            allow_any_command=body.get("allow_any_command") is True,
        )
        return _json(preview)

    async def module_update(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        module_id = request.path_params["module_id"]
        body = await self._body(request)
        try:
            module = await run_in_threadpool(
                self.app.modules.loader.update_server,
                module_id,
                body,
                allow_any_command=body.get("allow_any_command") is True,
            )
        except McpSetupError as exc:
            return _err(str(exc), exc.status)
        except ValueError as exc:
            return _err(str(exc), 400)
        return _json({"module": self._module_payload(module)})

    async def module_delete(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        module_id = request.path_params["module_id"]
        try:
            deleted = await run_in_threadpool(
                self.app.modules.loader.delete_server, module_id
            )
        except McpSetupError as exc:
            return _err(str(exc), exc.status)
        except ValueError as exc:
            return _err(str(exc), 400)
        return _json({"deleted": deleted})

    async def module_templates(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        templates = await run_in_threadpool(
            self.app.modules.loader.templates
        )
        return _json({"templates": templates})

    async def module_template_install(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        name = request.path_params["name"]
        body = await self._body(request)
        try:
            module = await run_in_threadpool(
                self.app.modules.loader.install_template,
                name,
                body.get("values") or {},
                module_name=(
                    str(body.get("name")) if body.get("name") else None
                ),
                domains=body.get("domains"),
                confirm=body.get("confirm") is True,
                allow_any_command=body.get("allow_any_command") is True,
            )
        except McpSetupError as exc:
            return _err(str(exc), exc.status)
        except ValueError as exc:
            return _err(str(exc), 400)
        relative = str(
            (module.provenance or {}).get("config_file") or ""
        )
        config_path = (
            str((self.app.data_dir / relative).resolve())
            if relative else ""
        )
        return _json({
            "module": self._module_payload(module),
            "config_path": config_path,
        })

    async def module_tool_risk(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        module_id = request.path_params["module_id"]
        body = await self._body(request)
        try:
            module = await run_in_threadpool(
                self.app.modules.loader.set_tool_risk,
                module_id,
                body.get("tool"),
                body.get("risk"),
            )
        except McpSetupError as exc:
            return _err(str(exc), exc.status)
        except ValueError as exc:
            return _err(str(exc), 400)
        return _json({"module": self._module_payload(module)})

    def _module_payload(self, module) -> dict[str, Any]:
        """Serialize one module without exposing private environment values."""
        tools = self.app.modules.tools(module.id)
        bindings = [
            binding for binding in self.app.modules.bindings(module.id)
        ]
        payload = {
            "id": module.id,
            "kind": module.kind,
            "name": module.name,
            "version": module.version,
            "status": module.status,
            "scope": module.scope,
            "origin": module.origin,
            "manifest": module.manifest,
            "provenance": module.provenance,
            "content_path": module.content_path,
            "last_loaded_at": module.last_loaded_at,
            "last_error": module.last_error,
            "usage_count": module.usage_count,
            "bound_domains": [
                binding.domain_id for binding in bindings
                if binding.enabled
            ],
            "bindings": [
                {"domain_id": binding.domain_id,
                 "enabled": binding.enabled,
                 "risk_override": binding.risk_override}
                for binding in bindings
            ],
            "tools": [
                {"name": tool.tool_name, "risk": tool.risk,
                 "side_effects": tool.side_effects,
                 "read_only": tool.read_only,
                 "last_seen": tool.last_seen}
                for tool in tools
            ],
            "health": self.app.modules.status.get(module.id, ""),
        }
        if module.kind == "mcp":
            loader = self.app.modules.loader
            payload.update({
                "env_names": [
                    str(name)
                    for name in module.manifest.get("env_names") or []
                ],
                "env_set": loader.env_set(module),
                "tool_risk": dict(module.manifest.get("tool_risk") or {}),
                "import_source": (
                    str((module.provenance or {}).get("source") or "")
                    if module.origin == "import" else ""
                ),
                # Imported rows are owned by their servers file; editing them
                # here would be discarded by the next sync reload.
                "editable": module.origin != "import",
            })
        return payload

    def _modules_payload(self) -> dict[str, Any]:
        modules = [
            self._module_payload(module)
            for module in self.app.modules.all()
        ]
        budget = int(
            self.app.config.get("modules.tool_schema_budget_tokens", 0)
        )
        selection = self.app.tools.schemas_for(
            None,
            budget_tokens=budget,
            eligible_modules=self.app.modules.eligible_module_ids(None),
        )
        return {
            "modules": modules,
            "templates": self.app.modules.loader.templates(),
            "command_allowlist": list(
                self.app.config.get("modules.command_allowlist", []) or []
            ),
            "schema_budget_tokens": budget,
            "schema_budget": {
                "tokens": selection["tokens"],
                "budget_tokens": selection["budget_tokens"],
                "tool_count": len(selection["names"]),
                "names": selection["names"],
                "exclusions": selection["exclusions"],
            },
        }

    async def modules_reload(self, request: Request) -> Response:
        """Reload every module without a restart."""
        if not self._authorized(request):
            return _err("unauthorized", 401)
        try:
            report = await run_in_threadpool(self.app.modules.reload)
        except Exception as exc:
            return _err(f"{type(exc).__name__}: {exc}", 500)
        return _json({"report": report})

    async def module_bindings(self, request: Request) -> Response:
        """Enable or disable one domain binding for a module."""
        if not self._authorized(request):
            return _err("unauthorized", 401)
        module_id = request.path_params["module_id"]
        module = self.app.modules.get(module_id)
        if module is None:
            return _err("no such module", 404)
        body = await self._body(request)
        domain_id = str(body.get("domain_id", "")).strip()
        if not domain_id or self.app.domains.get(domain_id) is None:
            return _err("no such domain", 404)
        enabled = bool(body.get("enabled", True))
        await run_in_threadpool(
            self.app.modules.set_binding,
            ModuleBinding(
                module_id=module_id, domain_id=domain_id, enabled=enabled,
            ),
        )
        fresh = self.app.modules.get(module_id)
        return _json({"module": {
            "id": module_id,
            "bound_domains": [
                binding.domain_id
                for binding in self.app.modules.bindings(module_id)
                if binding.enabled
            ],
            "status": fresh.status if fresh else module.status,
        }})

    async def module_action(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        module_id = request.path_params["module_id"]
        action = request.path_params["action"]
        module = self.app.modules.get(module_id)
        if module is None:
            return _err("no such module", 404)
        try:
            if action == "reload":
                result = await run_in_threadpool(
                    self.app.modules.reload, module_id
                )
                return _json({"module": result})
            if module.kind == "skill" and action == "rollback":
                updated = await run_in_threadpool(
                    self.app.modules.skills.rollback, module_id
                )
                return _json({
                    "module": self.app.modules.skills.describe(updated),
                })
            if action in ("enable", "activate", "disable"):
                enabled = action != "disable"
                updated = await run_in_threadpool(
                    self.app.modules.set_enabled, module_id, enabled,
                )
                payload = {
                    "id": updated.id, "status": updated.status,
                    "version": updated.version, "scope": updated.scope,
                }
                health = self.app.modules.status.get(module_id, "")
                return _json({"module": payload, "health": health})
        except ValueError as exc:
            return _err(str(exc), 404)
        except Exception as exc:
            return _err(f"{type(exc).__name__}: {exc}", 500)
        return _err("invalid action")

    async def skills_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json(await run_in_threadpool(self._skills_payload))

    def _skills_payload(self) -> dict[str, Any]:
        return {
            "skills": self.app.modules.skills.list_skills(),
            "enabled": bool(self.app.modules.skills.enabled),
            "auto_draft": bool(
                self.app.config.get("skills.auto_draft", True)
            ),
            "max_body_chars": self.app.modules.skills.max_body_chars(),
        }

    async def skill_detail(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        skill_id = request.path_params["skill_id"]
        module = self.app.modules.skills.get(skill_id)
        if module is None:
            return _err("no such skill", 404)
        return _json({
            "skill": self.app.modules.skills.describe(module),
            "body": self.app.modules.skills.body(module),
        })

    async def skills_create(self, request: Request) -> Response:
        """Create or revise a skill as a draft from the UI."""
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        try:
            module = await run_in_threadpool(
                self.app.modules.skills.create_draft,
                name=body.get("name"),
                description=body.get("description", ""),
                preconditions=body.get("preconditions"),
                domains=body.get("domains"),
                body=body.get("body", ""),
                origin="user",
                provenance={"source": "web"},
                created_by="user",
                reason=str(body.get("reason") or "created in the web UI"),
            )
        except ValueError as exc:
            return _err(str(exc), 400)
        return _json({"skill": self.app.modules.skills.describe(module)})

    async def skills_delete(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        skill_id = request.path_params["skill_id"]
        try:
            outcome = await run_in_threadpool(
                self.app.modules.skills.delete, skill_id
            )
        except ValueError as exc:
            return _err(str(exc), 409)
        return _json(outcome)

    async def consolidations_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json({
            "consolidations": self.app.consolidation.list_runs(limit=20),
            "schedule_interval_s": self.app.config.get(
                "consolidation.schedule_interval_s"
            ),
        })

    async def backups_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json({"backups": self._backups_payload()})

    def _backups_payload(self) -> list[dict[str, Any]]:
        directory = self.app.data_dir / "backups"
        if not directory.is_dir():
            return []
        items = []
        for path in sorted(
            directory.glob("*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            stat = path.stat()
            items.append({
                "name": path.name,
                "path": str(path),
                "size": stat.st_size,
                "mtime_ms": int(stat.st_mtime * 1000),
            })
        return items

    async def system_overview(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json(await run_in_threadpool(self._system_payload))

    def _system_payload(self) -> dict[str, Any]:
        app = self.app
        summary = self._summary_payload()
        return {
            "version": __version__,
            "git_sha": git_sha(),
            "schema_version": SCHEMA_VERSION,
            "data_dir": str(Path(app.data_dir).resolve()),
            "counts": summary["counts"],
            "working_task": summary["working_task"],
            "scheduler": summary["scheduler"],
            "domains": summary["domains"],
            "module_health": app.modules.status,
            "modules": [
                {"id": m.id, "name": m.name, "kind": m.kind,
                 "status": m.status, "last_error": m.last_error,
                 "health": app.modules.status.get(m.id, "")}
                for m in app.modules.all()
            ],
            "model": {
                "provider": configured_provider_id(app.config),
                "base_url": app.config.get("model.base_url"),
                "name": app.config.get("model.name"),
                "context_window": int(
                    app.config.get("model.context_window", 0) or 0
                ),
            },
            "now": util.iso(),
            "system_timezone": util.system_tz_name(),
        }

    async def tools_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json({
            "tools": [
                {"name": spec.name, "risk": spec.risk,
                 "module": spec.module,
                 "side_effects": spec.side_effects,
                 "read_only": spec.read_only,
                 "untrusted_output": spec.untrusted_output,
                 "risk_override": self.app.tools.risk_override(spec.name),
                 "summary_template": spec.summary_template}
                for spec in self.app.tools.specs()
            ],
            "mcp_status": self.app.modules.status,
        })

    async def tool_risk(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        tool_name = request.path_params["tool_name"]
        body = await self._body(request)
        if "risk" not in body:
            return _err("risk must be 'low', 'confirm', or null", 400)
        risk = body.get("risk")
        if risk not in ("low", "confirm", None):
            return _err("risk must be 'low', 'confirm', or null", 400)
        try:
            self.app.tools.set_risk_override(tool_name, risk)
        except ToolError as exc:
            return _err(str(exc), 400)
        spec = self.app.tools.resolve_optional(tool_name)
        if spec is None:
            return _err(f"unknown tool: {tool_name}", 404)
        return _json({
            "tool": tool_name,
            "risk": spec.risk,
            "risk_override": self.app.tools.risk_override(tool_name),
        })

    async def endpoint_probe(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json(await run_in_threadpool(self.app.model.probe))

    async def models_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json(await run_in_threadpool(self.app.model_catalogue))

    async def model_set(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        provider = str(body.get("provider") or "").strip() or None
        try:
            result = await run_in_threadpool(
                self.app.set_model, str(body.get("name") or ""), provider
            )
        except ModelSelectionError as exc:
            return _err(str(exc), exc.status)
        except (ConfigError, OSError, ValueError, TypeError) as exc:
            return _err(f"rejected: {exc}", 400)
        return _json(result)

    # -- config / maintenance -------------------------------------------------------

    async def config_get(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json({
            "config": self.app.config.redacted(),
            "editable_groups": sorted(EDITABLE_KEYS),
            "config_path": str(self.app.config.path or ""),
        })

    async def config_update(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        group = str(body.get("group", ""))
        value = body.get("value")
        try:
            await run_in_threadpool(
                self._apply_config_update, group, value
            )
        except (ConfigError, OSError, ValueError, TypeError) as exc:
            return _err(f"rejected: {exc}", 400)
        return _json({"status": "saved", "group": group,
                      "value": self.app.config.get(group)})

    def _apply_config_update(self, group: str, value: Any) -> None:
        merged = validate_group_update(self.app.config, group, value)
        self.app.config.set(group, merged)
        save_config(self.app.config)
        self.app.events.record(
            "config.updated", data={"group": group,
                                    "keys": sorted(value) if isinstance(value, dict)
                                    else []},
        )

    async def backup(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        path = await run_in_threadpool(self.app.backup)
        return _json({"backup": str(path)})

    async def restore(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        if not body.get("confirm"):
            return _err("restore requires confirm=true")
        archive = Path(str(body.get("path", "")))
        try:
            outcome = await run_in_threadpool(self.app.restore, archive)
        except (FileNotFoundError, ValueError) as exc:
            return _err(str(exc), 400)
        # The process must restart to reopen handles cleanly; the container
        # or service manager brings it back (restart: unless-stopped).
        threading.Timer(1.5, os._exit, [0]).start()
        return _json(outcome)

    async def reset(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        if body.get("confirm") is not True:
            return _err("reset requires confirm=true")
        try:
            outcome = await run_in_threadpool(
                self.app.reset,
                keep_config=bool(body.get("keep_config", False)),
            )
        except OSError as exc:
            return _err(str(exc), 500)
        # Only a real serve process holds the writer lock; tests build an App
        # without it and must not be terminated by a timer.
        if self.app._lock is not None:
            threading.Timer(1.5, os._exit, [0]).start()
        return _json(outcome)

    # ------------------------------------------------------------------

    def routes(self) -> Starlette:
        routes = [
            Route("/health", self.health, methods=["GET"]),
            Route("/", self.page, methods=["GET"]),
            Route("/assets/{name}", self.asset, methods=["GET"]),
            Route("/api/setup", self.setup_get, methods=["GET"]),
            Route("/api/setup", self.setup_submit, methods=["POST"]),
            Route("/api/setup/probe", self.setup_probe, methods=["POST"]),
            Route("/api/state", self.state, methods=["GET"]),
            Route("/api/summary", self.summary, methods=["GET"]),
            Route("/api/messages", self.messages, methods=["GET"]),
            Route("/api/events", self.events, methods=["GET"]),
            Route("/api/message", self.message, methods=["POST"]),
            Route("/api/tasks", self.tasks_list, methods=["GET"]),
            Route("/api/tasks/{task_id}", self.task_detail, methods=["GET"]),
            Route("/api/tasks/{task_id}/{action}", self.task_action,
                  methods=["POST"]),
            Route("/api/approvals", self.approvals_list, methods=["GET"]),
            Route("/api/approvals/{approval_id}/{decision}",
                  self.approval_decide, methods=["POST"]),
            Route("/api/schedules", self.schedules_list, methods=["GET"]),
            Route("/api/schedules", self.schedules_create, methods=["POST"]),
            Route("/api/schedules/{schedule_id}/{action}",
                  self.schedule_action, methods=["POST"]),
            Route("/api/inbox", self.inbox_receive, methods=["POST"]),
            Route("/api/inbox", self.inbox_list, methods=["GET"]),
            Route("/api/inbox/triage", self.inbox_triage, methods=["POST"]),
            Route("/api/inbox/{event_id}/ignore", self.inbox_ignore,
                  methods=["POST"]),
            Route("/api/automations", self.automations_create,
                  methods=["POST"]),
            Route("/api/automations", self.automations_list,
                  methods=["GET"]),
            Route("/api/automations/{automation_id}",
                  self.automation_update, methods=["PATCH"]),
            Route("/api/automations/{automation_id}/{action}",
                  self.automation_action, methods=["POST"]),
            Route("/api/memories", self.memories_list, methods=["GET"]),
            Route("/api/memories/search", self.memory_search, methods=["POST"]),
            Route("/api/memories/{memory_id}", self.memory_detail,
                  methods=["GET"]),
            Route("/api/memories/{memory_id}", self.memory_update,
                  methods=["PATCH"]),
            Route("/api/memories/{memory_id}", self.memory_delete,
                  methods=["DELETE"]),
            Route("/api/memories/{memory_id}/{action}", self.memory_action,
                  methods=["POST"]),
            Route("/api/domain-suggestions", self.domain_suggestions_list,
                  methods=["GET"]),
            Route("/api/domain-suggestions/{suggestion_id}/dismiss",
                  self.domain_suggestion_dismiss, methods=["POST"]),
            Route("/api/domains", self.domains_list, methods=["GET"]),
            Route("/api/domains", self.domains_create, methods=["POST"]),
            Route("/api/domains/feedback", self.domains_feedback,
                  methods=["GET"]),
            Route("/api/domains/route-preview", self.domains_route_preview,
                  methods=["POST"]),
            Route("/api/domains/{domain_id}", self.domain_update,
                  methods=["PATCH"]),
            Route("/api/heartbeat", self.heartbeat, methods=["POST"]),
            Route("/api/consolidate", self.consolidate, methods=["POST"]),
            Route("/api/consolidations", self.consolidations_list,
                  methods=["GET"]),
            Route("/api/modules", self.modules_list, methods=["GET"]),
            Route("/api/modules/templates", self.module_templates,
                  methods=["GET"]),
            Route("/api/modules/templates/{name}/install",
                  self.module_template_install, methods=["POST"]),
            Route("/api/modules/test", self.module_test, methods=["POST"]),
            Route("/api/modules/mcp", self.module_create, methods=["POST"]),
            Route("/api/modules/{module_id}", self.module_update,
                  methods=["PATCH"]),
            Route("/api/modules/{module_id}", self.module_delete,
                  methods=["DELETE"]),
            Route("/api/modules/reload", self.modules_reload, methods=["POST"]),
            Route("/api/modules/{module_id}/tool-risk", self.module_tool_risk,
                  methods=["POST"]),
            Route("/api/modules/{module_id}/bindings", self.module_bindings,
                  methods=["POST"]),
            Route("/api/modules/{module_id}/{action}", self.module_action,
                  methods=["POST"]),
            Route("/api/skills", self.skills_list, methods=["GET"]),
            Route("/api/skills", self.skills_create, methods=["POST"]),
            Route("/api/skills/{skill_id}", self.skill_detail,
                  methods=["GET"]),
            Route("/api/skills/{skill_id}", self.skills_delete,
                  methods=["DELETE"]),
            Route("/api/tools", self.tools_list, methods=["GET"]),
            Route("/api/tools/{tool_name}/risk", self.tool_risk,
                  methods=["POST"]),
            Route("/api/system", self.system_overview, methods=["GET"]),
            Route("/api/endpoint-probe", self.endpoint_probe, methods=["GET"]),
            Route("/api/models", self.models_list, methods=["GET"]),
            Route("/api/model", self.model_set, methods=["POST"]),
            Route("/api/config", self.config_get, methods=["GET"]),
            Route("/api/config", self.config_update, methods=["POST"]),
            Route("/api/backup", self.backup, methods=["POST"]),
            Route("/api/backups", self.backups_list, methods=["GET"]),
            Route("/api/restore", self.restore, methods=["POST"]),
            Route("/api/reset", self.reset, methods=["POST"]),
        ]
        return Starlette(routes=routes)


def serve(app: App, host: str | None = None, port: int | None = None) -> None:
    """Run the single process: web UI + API + scheduler + heartbeat."""
    import uvicorn

    app.startup(serve=True)
    app.scheduler.start()
    server = Server(app)
    uvicorn.run(
        server.routes(),
        host=host or str(app.config.get("server.host", "127.0.0.1")),
        port=port or int(app.config.get("server.port", 8080)),
        log_level="warning",
    )
    app.shutdown()

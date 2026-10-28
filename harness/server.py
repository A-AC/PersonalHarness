"""HTTP layer: JSON API + the web UI page. No logic of its own.

Every mutating endpoint delegates to the same App the CLI talks to over
HTTP, so the web UI and the terminal are two views of one runtime and one
state store. The UI page is a small polling client (see web_page.PAGE);
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

from . import util
from .app import App
from .config import EDITABLE_KEYS, ConfigError, save_config, validate_group_update
from .web_page import PAGE


def _json(data: Any, status: int = 200) -> Response:
    return JSONResponse(data, status_code=status)


def _err(message: str, status: int = 400) -> Response:
    return JSONResponse({"error": message}, status_code=status)


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
        if not self._authorized(request):
            return Response("unauthorized", status_code=401)
        response = Response(PAGE, media_type="text/html")
        return self._set_cookie(request, response)

    # -- state --------------------------------------------------------------

    async def state(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json(await run_in_threadpool(self._state_payload))

    def _state_payload(self) -> dict[str, Any]:
        app = self.app
        # Open work must never fall out of the UI because history is long:
        # show every open task first, then the most recent history.
        open_tasks = app.tasks.list_open()
        seen = {task["id"] for task in open_tasks}
        tasks = open_tasks + [
            task for task in app.tasks.list_recent(limit=60)
            if task["id"] not in seen
        ]
        approvals = app.approvals.pending()
        for approval in approvals:
            approval["summary"] = app.approvals.describe(approval)
        domains = app.domains.list_all()
        report = util.loads(
            app.db.meta_get("scheduler.last_report") or "", {}
        )
        return {
            "messages": app.messages.list_visible(limit=300),
            "tasks": tasks,
            "open_tasks": open_tasks,
            "approvals": approvals,
            "schedules": app.scheduler.list_schedules(),
            "domains": domains,
            "memories": app.memory.list(limit=50),
            "tools": [
                {"name": name,
                 "risk": app.tools._tools[name].risk,
                 "description": app.tools._tools[name].description[:160]}
                for name in app.tools.names()
            ],
            "events": app.events.list(limit=120),
            "config": app.config.redacted(),
            "persona": app.config.get("persona", {}),
            "consolidations": app.consolidation.list_runs(limit=5),
            "counts": {
                "open_tasks": len(open_tasks),
                "pending_approvals": len(approvals),
                "domains": len(domains),
                "paused": len([t for t in tasks if t["status"] == "paused"]),
            },
            "scheduler": {
                "last_tick": app.db.meta_get("scheduler.last_tick"),
                "last_report": report,
                "enabled": bool(app.config.get("scheduler.enabled", True)),
            },
            "now": util.iso(),
        }

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
            "SELECT id, status, trigger, started_at, ended_at, iterations,"
            " tool_calls, decision, error FROM runs WHERE task_id=?"
            " ORDER BY started_at",
            (task_id,),
        )
        executions = self.app.db.query(
            "SELECT * FROM tool_executions WHERE task_id=? ORDER BY"
            " started_at",
            (task_id,),
        )
        return _json({
            "task": task,
            "domain": self.app.domains.get(task.get("domain_id"))
            if task.get("domain_id") else None,
            "runs": [dict(r) for r in runs],
            "executions": [dict(r) for r in executions],
            "approvals": [
                a for a in self.app.approvals.pending(task_id)
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
            domain_id = str(body.get("domain_id", "")).strip()
            if self.app.domains.get(domain_id) is None:
                return _err("no such domain", 404)
            updated = self.app.tasks.set_domain(task_id, domain_id)
            return _json({"task": updated})
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
        if decision not in ("approve", "reject", "cancel"):
            return _err("invalid decision")
        approval = self.app.approvals.get(approval_id)
        if approval is None:
            return _err("no such approval", 404)
        try:
            result = await run_in_threadpool(
                self.app.runtime.decide_approval,
                approval_id, decision, "web",
            )
        except Exception as exc:
            return _err(str(exc), 409)
        return _json({
            "status": result.status, "message": result.message,
            "task_id": result.task_id, "approval_id": result.approval_id,
        })

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

    # -- memories ---------------------------------------------------------------

    async def memories_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        scope = request.query_params.get("scope")
        status = request.query_params.get("status", "active")
        payload = await run_in_threadpool(
            self._memories_payload, scope, status
        )
        return _json(payload)

    def _memories_payload(
        self, scope: str | None, status: str
    ) -> dict[str, Any]:
        return {
            "memories": self.app.memory.list(scope=scope, status=status),
            "artifact_problems": self.app.memory.check_artifacts(),
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
            self.app.memory.search, query, scope_priority=priority
        )
        history = await run_in_threadpool(
            self.app.messages.search, query,
            limit=int(self.app.config.get("context.history_results", 4)),
        )
        return _json({"results": results, "history": history})

    # -- domains ------------------------------------------------------------------

    async def domains_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json({"domains": self.app.domains.list_all()})

    async def domains_create(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        body = await self._body(request)
        name = str(body.get("name", "")).strip()
        if not name:
            return _err("name required")
        if self.app.domains.by_name(name):
            return _err("a domain with that name exists", 409)
        domain_id = util.new_id("dom")
        domain = self.app.domains.create(
            domain_id, name=name,
            description=str(body.get("description", "")),
            created_by="web", seed_note=body.get("note") or None,
        )
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
            domain = self.app.domains.update(
                domain_id,
                name=name,
                description=(
                    str(body["description"])
                    if "description" in body else None
                ),
                status=body.get("status"),
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

    # -- tools / endpoint diagnostics --------------------------------------------

    async def tools_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json({
            "tools": [
                {"name": name, "risk": self.app.tools._tools[name].risk}
                for name in self.app.tools.names()
            ],
            "mcp_status": self.app.mcp.status,
        })

    async def endpoint_probe(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json(await run_in_threadpool(self.app.model.probe))

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

    # ------------------------------------------------------------------

    def routes(self) -> Starlette:
        routes = [
            Route("/health", self.health, methods=["GET"]),
            Route("/", self.page, methods=["GET"]),
            Route("/api/state", self.state, methods=["GET"]),
            Route("/api/events", self.events, methods=["GET"]),
            Route("/api/message", self.message, methods=["POST"]),
            Route("/api/tasks/{task_id}", self.task_detail, methods=["GET"]),
            Route("/api/tasks/{task_id}/{action}", self.task_action,
                  methods=["POST"]),
            Route("/api/approvals/{approval_id}/{decision}",
                  self.approval_decide, methods=["POST"]),
            Route("/api/schedules", self.schedules_list, methods=["GET"]),
            Route("/api/schedules", self.schedules_create, methods=["POST"]),
            Route("/api/schedules/{schedule_id}/{action}",
                  self.schedule_action, methods=["POST"]),
            Route("/api/memories", self.memories_list, methods=["GET"]),
            Route("/api/memories/search", self.memory_search, methods=["POST"]),
            Route("/api/memories/{memory_id}", self.memory_detail,
                  methods=["GET"]),
            Route("/api/domains", self.domains_list, methods=["GET"]),
            Route("/api/domains", self.domains_create, methods=["POST"]),
            Route("/api/domains/{domain_id}", self.domain_update,
                  methods=["PATCH"]),
            Route("/api/heartbeat", self.heartbeat, methods=["POST"]),
            Route("/api/consolidate", self.consolidate, methods=["POST"]),
            Route("/api/tools", self.tools_list, methods=["GET"]),
            Route("/api/endpoint-probe", self.endpoint_probe, methods=["GET"]),
            Route("/api/config", self.config_get, methods=["GET"]),
            Route("/api/config", self.config_update, methods=["POST"]),
            Route("/api/backup", self.backup, methods=["POST"]),
            Route("/api/restore", self.restore, methods=["POST"]),
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

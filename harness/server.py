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

from . import util
from .app import App
from .config import EDITABLE_KEYS, ConfigError, save_config, validate_group_update
from .inbox import InboxError, InboxTooLarge
from .memory import KINDS as MEMORY_KINDS
from .modules.types import ModuleBinding

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

    def _approvals_payload(self) -> dict[str, Any]:
        approvals = self.app.approvals.pending()
        for approval in approvals:
            approval["summary"] = self.app.approvals.describe(approval)
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
            "runs": runs_payload,
            "toolset": latest_toolset,
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
        try:
            limit = int(request.query_params.get("limit", 200))
        except ValueError:
            return _err("limit must be an integer")
        limit = max(1, min(limit, 1000))
        payload = await run_in_threadpool(
            self._memories_payload, scope, status, limit
        )
        return _json(payload)

    def _memories_payload(
        self, scope: str | None, status: str, limit: int = 200
    ) -> dict[str, Any]:
        return {
            "memories": self.app.memory.list(
                scope=scope, status=status, limit=limit
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

    # -- modules / tools / endpoint diagnostics -----------------------------------

    async def modules_list(self, request: Request) -> Response:
        if not self._authorized(request):
            return _err("unauthorized", 401)
        return _json(await run_in_threadpool(self._modules_payload))

    def _modules_payload(self) -> dict[str, Any]:
        modules = []
        for module in self.app.modules.all():
            tools = self.app.modules.tools(module.id)
            bindings = [
                binding for binding in self.app.modules.bindings(module.id)
            ]
            modules.append({
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
            })
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
                "base_url": app.config.get("model.base_url"),
                "name": app.config.get("model.name"),
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
                 "untrusted_output": spec.untrusted_output}
                for spec in self.app.tools.specs()
            ],
            "mcp_status": self.app.modules.status,
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
            Route("/assets/{name}", self.asset, methods=["GET"]),
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
            Route("/api/domains", self.domains_list, methods=["GET"]),
            Route("/api/domains", self.domains_create, methods=["POST"]),
            Route("/api/domains/{domain_id}", self.domain_update,
                  methods=["PATCH"]),
            Route("/api/heartbeat", self.heartbeat, methods=["POST"]),
            Route("/api/consolidate", self.consolidate, methods=["POST"]),
            Route("/api/consolidations", self.consolidations_list,
                  methods=["GET"]),
            Route("/api/modules", self.modules_list, methods=["GET"]),
            Route("/api/modules/reload", self.modules_reload, methods=["POST"]),
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
            Route("/api/system", self.system_overview, methods=["GET"]),
            Route("/api/endpoint-probe", self.endpoint_probe, methods=["GET"]),
            Route("/api/config", self.config_get, methods=["GET"]),
            Route("/api/config", self.config_update, methods=["POST"]),
            Route("/api/backup", self.backup, methods=["POST"]),
            Route("/api/backups", self.backups_list, methods=["GET"]),
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

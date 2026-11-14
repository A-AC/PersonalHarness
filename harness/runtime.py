"""The agent runtime: one bounded, reusable execution loop.

Every trigger — web message, CLI message, scheduled prompt, heartbeat work,
approval decision, restart recovery — enters this loop. There is no second
implementation of reasoning anywhere in the system.

The loop persists the run transcript before advancing, so a run paused by an
approval, a question, a limit, or a crash resumes exactly where it stopped
without repeating completed actions. Tool calls are validated and executed by
the harness (see tools.py); confirm-risk actions become approvals bound to
the exact arguments; outcomes are recorded as they are, including unknown.
"""

from __future__ import annotations

import functools
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import util
from . import transcript as transcript_mod
from .approvals import ApprovalError
from .config import Config
from .db import Database, rows_to_dicts
from .model import (
    ASK_USER_TAG,
    Model,
    ModelContextError,
    ModelEmptyReply,
    ModelResponseError,
    ModelUnavailable,
    ToolCall,
)
from .store import Domains, Events, Messages, Tasks


SKILL_WRITE_TOOLS = frozenset({"propose_skill", "edit_skill"})


@dataclass
class RunResult:
    status: str
    run_id: str
    task_id: str
    message: str = ""
    assistant_message_id: int | None = None
    approval_id: str | None = None
    detail: str = ""


def _serialized(method):
    """Serialize every runtime entry point on the single run lock."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class AgentRuntime:
    """Serialized execution: one run advances at a time."""

    def __init__(self, app):
        self.app = app
        self.db: Database = app.db
        self.config: Config = app.config
        self.model: Model = app.model
        self.messages: Messages = app.messages
        self.events: Events = app.events
        self.domains: Domains = app.domains
        self.tasks: Tasks = app.tasks
        self.memory = app.memory
        self.tools = app.tools
        self.approvals = app.approvals
        self.context = app.context
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Run persistence helpers
    # ------------------------------------------------------------------

    def _run_row(self, run_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM runs WHERE id=?", (run_id,))
        return rows_to_dicts([row])[0] if row else None

    def _save_run(self, run_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(
            f"UPDATE runs SET {sets} WHERE id=?", (*fields.values(), run_id)
        )

    def _transcript(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        return util.loads(run.get("transcript") or "[]", [])

    def _append_transcript(
        self, run_id: str, message: dict[str, Any]
    ) -> None:
        run = self._run_row(run_id)
        transcript = self._transcript(run)
        transcript.append(message)
        self._save_run(run_id, transcript=util.dumps(transcript))

    def _latest_run(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
            (task_id,),
        )
        return rows_to_dicts([row])[0] if row else None

    def _prompt_budget_tokens(self) -> int:
        """Messages + tool schemas must fit this many tokens."""
        window = int(self.config.get("model.context_window", 0) or 0)
        if window > 0:
            reserved = int(
                self.config.get("context.reserved_output_tokens", 2048)
            )
            return max(1000, window - reserved)
        return int(self.config.get("context.total_budget_tokens", 20000))

    def _schema_budget(self) -> int:
        return max(0, int(
            self.config.get("modules.tool_schema_budget_tokens", 2400)
        ))

    def _limits_for(self, task: dict[str, Any]) -> tuple[int, int, float]:
        """Iteration, tool-call, and wall-clock budgets for this task.

        A child run always gets the tighter sub-agent budget recorded on its
        task meta (with config as the fallback), never the parent's budget.
        """
        if (task.get("role") or "user") == "subagent":
            meta = util.loads(task.get("meta") or "{}", {})
            budget = meta.get("budget") if isinstance(meta, dict) else {}
            budget = budget if isinstance(budget, dict) else {}
            return (
                int(budget.get(
                    "max_iterations",
                    self.config.get("subagents.max_iterations", 8),
                )),
                int(budget.get(
                    "max_tool_calls",
                    self.config.get("subagents.max_tool_calls", 12),
                )),
                float(budget.get(
                    "max_seconds",
                    self.config.get("subagents.max_seconds", 180.0),
                )),
            )
        return (
            int(self.config.get("limits.max_iterations", 16)),
            int(self.config.get("limits.max_tool_calls", 24)),
            float(self.config.get("limits.max_run_seconds", 600)),
        )

    @staticmethod
    def _visible(task: dict[str, Any] | None) -> bool:
        """Child messages stay inspectable but out of the ordinary chat."""
        return bool(task) and (task.get("role") or "user") != "subagent"

    @staticmethod
    def _toolset_snapshot(selection: dict[str, Any]) -> dict[str, Any]:
        """The durable part of a capability selection (never the schemas)."""
        return {
            key: selection[key]
            for key in (
                "domain_id", "role", "names", "modules", "exclusions",
                "tokens", "budget_tokens",
            )
        }

    def _run_meta(self, run_id: str) -> dict[str, Any]:
        run = self._run_row(run_id)
        meta = util.loads(run.get("meta") or "", {}) if run else {}
        return meta if isinstance(meta, dict) else {}

    def _save_meta(self, run_id: str, meta: dict[str, Any]) -> None:
        self._save_run(run_id, meta=util.dumps(meta))

    def _toolset(self, run_id: str) -> dict[str, Any]:
        snapshot = self._run_meta(run_id).get("toolset")
        return snapshot if isinstance(snapshot, dict) else {}

    def _allowed_tool_names(self, run_id: str) -> set[str] | None:
        """The run snapshot's tool names, or ``None`` before one exists."""
        names = self._toolset(run_id).get("names")
        return set(names) if isinstance(names, list) else None

    def _select_tools(
        self, domain_id: str | None, role: str = "user"
    ) -> dict[str, Any]:
        """Resolve eligible modules, then apply the schema budget."""
        exclude = (
            {"delegate_task"}
            if not bool(self.config.get("subagents.enabled", True))
            else None
        )
        return self.tools.schemas_for(
            domain_id,
            budget_tokens=self._schema_budget(),
            eligible_modules=self.app.modules.eligible_module_ids(domain_id),
            role=role,
            exclude_names=exclude,
        )

    def _save_toolset(
        self, run_id: str, selection: dict[str, Any]
    ) -> None:
        meta = self._run_meta(run_id)
        meta["toolset"] = self._toolset_snapshot(selection)
        self._save_meta(run_id, meta)

    def _append_context_source(
        self, run_id: str, source: dict[str, Any]
    ) -> None:
        run = self._run_row(run_id)
        sources = util.loads(run.get("context_sources") or "", []) if run else []
        if not isinstance(sources, list):
            sources = []
        sources.append(source)
        self._save_run(run_id, context_sources=util.dumps(sources))

    def _record_toolset_change(
        self, run_id: str, previous: dict[str, Any], selection: dict[str, Any]
    ) -> None:
        """The domain changed mid-run; recompute and explain the change."""
        old_domain = previous.get("domain_id")
        new_domain = selection.get("domain_id")
        detail = (
            f"task domain changed from {old_domain!r} to {new_domain!r}; "
            "the toolset was recomputed for the run"
        )
        self.events.record(
            "toolset.changed", run_id=run_id,
            data={
                "from_domain_id": old_domain,
                "to_domain_id": new_domain,
                "names": selection["names"],
                "modules": selection["modules"],
            },
        )
        self._append_context_source(
            run_id,
            {"section": "tools", "ref": "toolset.changed", "reason": detail},
        )
        for exclusion in selection["exclusions"]:
            self._append_context_source(run_id, {
                "section": "tools",
                "ref": exclusion["tool"],
                "reason": f"excluded: {exclusion['detail']}",
            })

    def _run_schemas(
        self, run: dict[str, Any], task: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Schemas for this run: its snapshot, or one computed and saved now.

        A module reload or a domain switch cannot silently change the toolset
        mid-run. The one allowed change is an explicit domain change (for
        example ``use_domain``): it recomputes once and records the event and
        a context source. Older runs without a snapshot get one on first use.
        """
        domain_id = task.get("domain_id") if task else None
        role = (task.get("role") if task else None) or "user"
        snapshot = self._toolset(run["id"])
        if isinstance(snapshot.get("names"), list):
            if snapshot.get("domain_id") != domain_id:
                selection = self._select_tools(domain_id, role)
                self._save_toolset(run["id"], selection)
                self._record_toolset_change(run["id"], snapshot, selection)
                return selection["schemas"]
            return self.tools.schemas_for_names(snapshot["names"])
        selection = self._select_tools(domain_id, role)
        self._save_toolset(run["id"], selection)
        return selection["schemas"]

    # -- trust / taint -----------------------------------------------------

    def _taint_sources(self, run_id: str) -> list[dict[str, Any]]:
        sources = self._run_meta(run_id).get("tainted_sources")
        return sources if isinstance(sources, list) else []

    def _taint_origin(self, result: Any) -> str:
        """Name the external origin an approval prompt must identify."""
        if isinstance(result, dict):
            sources = result.get("sources")
            if isinstance(sources, list):
                urls = [str(url) for url in sources if str(url).strip()]
                if urls:
                    return urls[0]
            for key in ("url", "source", "origin"):
                if result.get(key):
                    return util.clip(str(result[key]), 300)
            if result.get("query"):
                return f"web results for '{result['query']}'"
            content = result.get("content")
            if isinstance(content, str) and content.strip():
                return util.clip(content.strip(), 300)
        return util.clip(str(result or ""), 300)

    def _mark_tainted(
        self, run_id: str, tool: str, module: str, origin: str
    ) -> dict[str, Any]:
        """Record untrusted content on the run (bounded, newest last)."""
        meta = self._run_meta(run_id)
        sources = meta.get("tainted_sources")
        if not isinstance(sources, list):
            sources = []
        entry = {
            "tool": tool,
            "module": module,
            "origin": util.clip(origin, 300),
            "at": util.iso(),
        }
        if sources and all(
            sources[-1].get(key) == entry[key]
            for key in ("tool", "origin")
        ):
            return sources[-1]
        sources.append(entry)
        meta["tainted_sources"] = sources[-20:]
        self._save_meta(run_id, meta)
        self.events.record(
            "trust.tainted", run_id=run_id,
            data={"tool": tool, "module": module, "origin": entry["origin"]},
        )
        return entry

    def _prepare_prompt(
        self,
        transcript: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
        """Repair protocol shape, then compact to the model's window.

        Repairs are persisted by the caller (they fix invalid transcripts);
        compaction is per-call and never rewrites stored history.
        """
        repaired, repairs = transcript_mod.repair(transcript)
        prompt, compacted = transcript_mod.compact(
            repaired, self._prompt_budget_tokens(), tools_schema
        )
        return prompt, repairs + compacted, repaired

    # ------------------------------------------------------------------
    # Entry: user message from any interface
    # ------------------------------------------------------------------

    def submit_user_message(
        self, text: str, source: str = "web", domain_id: str | None = None
    ) -> RunResult:
        """The single entry point for a human message (web or CLI)."""
        text = (text or "").strip()
        if not text:
            raise ValueError("empty message")
        if domain_id and self.domains.get(domain_id) is None:
            raise ValueError(f"no such domain: {domain_id}")
        with self._lock:
            # 1. Quick confirmation on a pending approval.
            pending = self.approvals.pending()
            if pending:
                quick = self.approvals.maybe_quick_confirm(text, pending)
                if quick and quick["kind"] == "decision":
                    return self._apply_quick_decision(
                        text, source, quick, pending
                    )
                if (
                    quick
                    and quick["kind"] in ("ambiguous", "ambiguous_multiple")
                    and not self._looks_like_question(text)
                ):
                    return self._ask_which_approval(text, source, quick)

            # 2. Record the message, then route.
            message_id = self.messages.record(
                "user", text, source=source, visible=True
            )
            result = self._route_and_run(text, source, message_id, domain_id)
            self.db.execute(
                "UPDATE messages SET task_id=?, domain_id=? WHERE id=?",
                (result.task_id, self._task_domain(result.task_id), message_id),
            )
            return result

    def _looks_like_question(self, text: str) -> bool:
        stripped = (text or "").strip().lower()
        if "?" in stripped:
            return True
        words = stripped.split()
        first = words[0] if words else ""
        return first in {
            "what", "why", "how", "when", "where", "who", "which", "whose",
            "does", "do", "did", "can", "could", "will", "would", "should",
            "is", "are", "was", "were", "explain", "tell",
        }

    def _ask_which_approval(
        self, text: str, source: str, quick: dict[str, Any]
    ) -> RunResult:
        """A reply was not a clear decision: ask, never guess.

        This is a visible, deterministic note — no model call, no execution,
        and every pending approval stays exactly as it was.
        """
        message_id = self.messages.record("user", text, source=source)
        pending = quick.get("pending") or []
        first = pending[0] if pending else None
        if quick["kind"] == "ambiguous_multiple":
            lines = ["I have more than one action waiting for your decision:"]
            for approval in pending:
                lines.append(f"- {self.approvals.describe(approval)}")
            lines.append(
                "Reply with `approve` or `reject` and name the action (for "
                "example `approve Logs`), or use the Approvals panel."
            )
        else:
            summary = quick.get("summary") or (
                self.approvals.describe(pending[0]) if pending else "the action"
            )
            lines = [
                "I did not apply that reply: it is not a clear yes or no for "
                f"{summary}.",
                "Reply `approve` or `reject` (or use the Approvals panel). "
                "Nothing was executed.",
            ]
        note = "\n".join(lines)
        task_id = first["task_id"] if first else None
        run_id = first["run_id"] if first else ""
        self.messages.record(
            "note", note, source="system", task_id=task_id,
            run_id=run_id or None,
            domain_id=self._task_domain(task_id) if task_id else None,
        )
        if task_id:
            self.db.execute(
                "UPDATE messages SET task_id=?, domain_id=? WHERE id=?",
                (task_id, self._task_domain(task_id), message_id),
            )
        self.events.record(
            "approval.ambiguous", task_id=task_id,
            data={"kind": quick["kind"], "text": text[:200]},
        )
        return RunResult(
            "waiting_approval", run_id, task_id or "", message=note,
        )

    def _task_domain(self, task_id: str) -> str | None:
        task = self.tasks.get(task_id)
        return task["domain_id"] if task else None

    def _apply_quick_decision(
        self,
        text: str,
        source: str,
        quick: dict[str, Any],
        pending: list[dict[str, Any]],
    ) -> RunResult:
        """A short affirmative/negative reply resolves exactly one approval.

        The classifier only picked the label; the decision is still bound to
        the exact pending action, which must still be valid and unchanged.
        """
        message_id = self.messages.record("user", text, source=source)
        approval = self.approvals.get(quick["approval_id"])
        assert approval is not None
        self.db.execute(
            "UPDATE messages SET task_id=?, domain_id=? WHERE id=?",
            (approval["task_id"], self._task_domain(approval["task_id"]),
             message_id),
        )
        try:
            self.approvals.decide(
                approval["id"], quick["decision"], decided_by=f"quick:{source}"
            )
        except Exception as exc:
            # Expired or already resolved: record honestly, run nothing.
            self.messages.record(
                "note", f"That approval could not be applied: {exc}",
                source="system", task_id=approval["task_id"], visible=True,
            )
            return RunResult(
                "waiting_approval", approval["run_id"], approval["task_id"],
                message=str(exc),
            )
        if quick["decision"] == "approve":
            return self.resume_after_approval(approval, source)
        return self.resume_after_rejection(approval, source)

    # ------------------------------------------------------------------
    # Routing: domain, task resume, new task
    # ------------------------------------------------------------------

    def _route_and_run(
        self, text: str, source: str, message_id: int,
        domain_override: str | None = None,
    ) -> RunResult:
        # Route first: the routed domain is evidence for which open task this
        # message continues. Doing it the other way around let a resume
        # candidate from an unrelated domain win.
        if domain_override:
            domain_id = domain_override
            self.events.record(
                "domain.routed", domain_id=domain_id,
                data={"reason": "explicit domain from the user",
                      "candidates": []},
            )
        else:
            domain_id = self._route_domain(text)
        candidate = self.tasks.find_resume_candidate(text, domain_id=domain_id)

        if candidate.get("ambiguous"):
            # Competing candidates are materially ambiguous: create a new
            # task that asks the user which one to continue.
            task = self.tasks.create(
                objective=text, domain_id=domain_id,
                origin="user", origin_ref=str(message_id),
                meta={"notes": "", "ambiguous_candidates": candidate["ambiguous"]},
            )
            return self.start_run(task, {"kind": "user"}, message_id)

        existing = candidate.get("task")
        if existing is not None:
            # Prefer resuming plausibly-continued work over creating new work.
            self.events.record(
                "task.resumed", task_id=existing["id"],
                data={"reason": "message continues this task",
                      "score": candidate.get("score")},
            )
            self.tasks.touch(existing["id"])
            if existing["status"] == "waiting_user":
                return self.resume_with_user_answer(existing, text, message_id)
            return self.resume_task(existing, text, message_id)

        task = self.tasks.create(
            objective=text, domain_id=domain_id, origin="user",
            origin_ref=str(message_id), meta={"notes": ""},
        )
        return self.start_run(task, {"kind": "user"}, message_id)

    def _route_domain(self, text: str) -> str:
        routing = self.domains.route(
            text,
            open_tasks=self.tasks.list_open(),
            recent_messages=self.messages.recent(limit=10),
        )
        self.events.record(
            "domain.routed",
            domain_id=routing["domain_id"],
            data={
                "reason": routing["reason"],
                "candidates": routing["candidates"],
            },
        )
        return routing["domain_id"]

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    @_serialized
    def start_run(
        self, task: dict[str, Any], trigger: dict[str, Any],
        trigger_message_id: int | None = None,
        *,
        trigger_text: str | None = None,
    ) -> RunResult:
        """Create a run for a task and execute it to a pause or completion."""
        run_id = util.new_id("run")
        pending = self.approvals.pending()
        built = self.context.build(
            task=task, domain=self.domains.get(task["domain_id"])
            if task.get("domain_id") else None,
            trigger=trigger, pending_approvals=pending, run_id=run_id,
        )
        role = task.get("role") or "user"
        selection = self._select_tools(task.get("domain_id"), role)
        sources = list(built["sources"])
        for exclusion in selection["exclusions"]:
            sources.append({
                "section": "tools",
                "ref": exclusion["tool"],
                "reason": f"excluded: {exclusion['detail']}",
            })
        user_text = trigger_text or (
            f"[{trigger['kind']}] {trigger.get('name') or ''}".strip()
            if trigger.get("kind") in ("schedule", "heartbeat")
            else task["objective"]
        )
        transcript = [
            {"role": "system", "content": built["system"]},
            {"role": "user", "content": user_text},
        ]
        parent_run_id = trigger.get("parent_run_id") or task.get("parent_run_id")
        run_meta: dict[str, Any] = {
            "toolset": self._toolset_snapshot(selection),
        }
        # Inbox content is untrusted by definition. Seed the same taint list
        # that untrusted tool output uses, so every later side-effecting tool
        # is escalated to an explicit approval by the one existing rule.
        tainted: list[dict[str, Any]] = []
        task_meta = util.loads(task.get("meta") or "{}", {})
        for source in (
            list(trigger.get("tainted_sources") or [])
            + list((task_meta or {}).get("tainted_sources") or [])
        ):
            if not isinstance(source, dict):
                continue
            entry = {
                "tool": str(source.get("tool") or "inbox"),
                "module": str(source.get("module") or "inbox"),
                "origin": util.clip(str(source.get("origin") or "external"), 300),
                "at": source.get("at") or util.iso(),
            }
            if any(
                existing.get("tool") == entry["tool"]
                and existing.get("origin") == entry["origin"]
                for existing in tainted
            ):
                continue
            tainted.append(entry)
        if tainted:
            run_meta["tainted_sources"] = tainted[-20:]
        self.db.execute(
            "INSERT INTO runs(id, task_id, trigger, trigger_ref, status,"
            " started_at, iterations, tool_calls, transcript, context_sources,"
            " parent_run_id, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, task["id"], trigger["kind"],
                str(trigger_message_id) if trigger_message_id else None,
                "running", util.iso(), 0, 0, util.dumps(transcript),
                util.dumps(sources), parent_run_id, util.dumps(run_meta),
            ),
        )
        self.events.record(
            "run.started", task_id=task["id"], run_id=run_id,
            data={"trigger": trigger["kind"]},
        )
        self.tasks.set_status(task["id"], "active", reason="run started")
        run = self._run_row(run_id)
        return self._loop(run, trigger)

    @_serialized
    def resume_task(
        self, task: dict[str, Any], text: str, message_id: int
    ) -> RunResult:
        """Continue an open (active/paused) task with a new user message."""
        run = self._latest_run(task["id"])
        if run is None or run["status"] in ("completed", "failed"):
            return self.start_run(
                task, {"kind": "user"}, message_id
            )
        if run["status"] == "waiting_approval":
            return self._ask_while_approval_pending(task, text, message_id)
        self.tasks.clear_retry(task["id"])
        self._append_transcript(
            run["id"], {"role": "user", "content": text}
        )
        self._save_run(run["id"], status="running", ended_at=None,
                       decision=None)
        self.tasks.set_status(task["id"], "active", reason="resumed by user")
        run = self._run_row(run["id"])
        return self._loop(run, {"kind": "user"})

    @_serialized
    def resume_with_user_answer(
        self, task: dict[str, Any], text: str, message_id: int
    ) -> RunResult:
        """The task asked a question; the user's reply is the answer."""
        run = self._latest_run(task["id"])
        if run is None or run["status"] not in ("waiting_user", "interrupted"):
            return self.resume_task(task, text, message_id)
        self.tasks.clear_retry(task["id"])
        self._append_transcript(run["id"], {"role": "user", "content": text})
        self._save_run(run["id"], status="running", ended_at=None)
        self.tasks.set_status(task["id"], "active", reason="user answered")
        run = self._run_row(run["id"])
        return self._loop(run, {"kind": "user"})

    def _ask_while_approval_pending(
        self, task: dict[str, Any], text: str, message_id: int
    ) -> RunResult:
        """The user sent a normal message while an approval is pending. The
        approval stays pending; the question is answered without executing
        the unapproved action."""
        run = self._latest_run(task["id"])
        assert run is not None
        self._append_transcript(
            run["id"],
            {"role": "user", "content": (
                f"[user message while approval is pending] {text}"
            )},
        )
        self._save_run(run["id"], status="running", ended_at=None)
        run = self._run_row(run["id"])
        return self._loop(run, {"kind": "user"})

    # -- approval-driven resumes -----------------------------------------

    @_serialized
    def decide_approval(
        self, approval_id: str, decision: str, source: str = "web"
    ) -> RunResult:
        """Decide an approval and run its consequence under one lock.

        This keeps the decision and the claim/execution from interleaving
        with a user message or a scheduler tick.
        """
        approval = self.approvals.get(approval_id)
        if approval is None:
            raise ValueError(f"no such approval: {approval_id}")
        decided = self.approvals.decide(
            approval_id, decision, decided_by=source
        )
        if decision == "approve":
            return self.resume_after_approval(decided, source)
        return self.resume_after_rejection(decided, source)

    def resume_approved_approvals(self) -> list[RunResult]:
        """Execute approvals that were granted just before a crash."""
        results: list[RunResult] = []
        for approval in self.approvals.approved_awaiting_execution():
            try:
                results.append(
                    self.resume_after_approval(approval, "recovery")
                )
            except Exception as exc:  # one stuck approval must not kill the tick
                self.events.record(
                    "approval.resume_failed", level="error",
                    task_id=approval["task_id"], run_id=approval["run_id"],
                    data={"approval_id": approval["id"],
                          "error": f"{type(exc).__name__}: {exc}"},
                )
        return results

    @_serialized
    def resume_after_approval(
        self, approval: dict[str, Any], source: str
    ) -> RunResult:
        """Approval granted: execute exactly that action, then continue.

        The authorization is still bound to the exact pending action: the
        awaiting execution row for this approval must still be awaiting, and
        the run must still be waiting on it. Anything else is stale and
        executes nothing.
        """
        # Re-read the approval so execution uses the latest refined arguments
        # even if the caller held an older snapshot of the request.
        approval = self.approvals.get(approval["id"]) or approval
        run = self._run_row(approval["run_id"])
        task = self.tasks.get(approval["task_id"])
        if (
            run is None or task is None
            or run["status"] != "waiting_approval"
        ):
            # Stale authorization: never claim or execute it.
            self.events.record(
                "approval.stale", level="warn", task_id=approval["task_id"],
                data={"approval_id": approval["id"],
                      "run_status": run["status"] if run else "missing"},
            )
            return RunResult(
                "stale", approval["run_id"], approval["task_id"],
                message="That approval no longer matches the pending action; "
                        "nothing was executed.",
            )
        execution_id = self.approvals.claim_for_execution(approval["id"])
        if execution_id is None:
            self.events.record(
                "approval.stale", level="warn", task_id=approval["task_id"],
                data={"approval_id": approval["id"],
                      "run_status": run["status"]},
            )
            return RunResult(
                "stale", approval["run_id"], approval["task_id"],
                message="That approval no longer matches the pending action; "
                        "nothing was executed.",
            )
        call = ToolCall(
            id=approval.get("tool_call_id") or util.new_id("tc"),
            name=approval["tool"],
            arguments=approval["arguments"],
            raw_arguments=util.dumps(approval["arguments"]),
        )
        ctx = self.app.tool_context(run["id"], task["id"], task.get("domain_id"))
        allowed = self._allowed_tool_names(run["id"])
        outcome = self.tools.execute(
            call, ctx, run_id=run["id"], task_id=task["id"],
            execution_id=execution_id, approval_id=approval["id"],
            allowed_names=allowed, role=task.get("role") or "user",
        )
        if outcome.get("status") == "succeeded":
            spec = self.tools.resolve_optional(call.name)
            if spec is not None and spec.untrusted_output:
                self._mark_tainted(
                    run["id"], call.name, spec.module,
                    self._taint_origin(outcome.get("result")),
                )
        self.approvals.mark_executed(approval["id"])
        self._append_tool_result(
            run["id"], call.id, outcome, approval["id"]
        )
        self._save_run(
            run["id"], status="running", ended_at=None,
            tool_calls=int(run["tool_calls"]) + 1,
        )
        self.tasks.set_status(task["id"], "active", reason="approved")
        run = self._run_row(run["id"])
        return self._loop(run, {"kind": "approval"})

    @_serialized
    def resume_after_rejection(
        self, approval: dict[str, Any], source: str
    ) -> RunResult:
        run = self._run_row(approval["run_id"])
        task = self.tasks.get(approval["task_id"])
        if run is None or task is None or run["status"] != "waiting_approval":
            return RunResult(
                "stale", approval["run_id"], approval["task_id"],
                message="That approval no longer matches the pending action.",
            )
        call_id = approval.get("tool_call_id") or "rejected_call"
        self._append_tool_result(
            run["id"], call_id,
            {"status": "denied",
             "error": "The user did not approve this action. Do not attempt "
                      "it again unless the user asks for it anew."},
            approval["id"],
        )
        execution_id = self._awaiting_execution_id(approval["id"])
        if execution_id:
            self.db.execute(
                "UPDATE tool_executions SET status='denied', ended_at=?,"
                " error='rejected by user' WHERE id=?",
                (util.iso(), execution_id),
            )
        self._save_run(run["id"], status="running", ended_at=None)
        self.tasks.set_status(task["id"], "active", reason="rejected")
        run = self._run_row(run["id"])
        return self._loop(run, {"kind": "approval"})

    def _awaiting_execution_id(self, approval_id: str) -> str | None:
        row = self.db.query_one(
            "SELECT id FROM tool_executions WHERE approval_id=? AND"
            " status='awaiting_approval' LIMIT 1",
            (approval_id,),
        )
        return row["id"] if row else None

    # -- recovery resumes -------------------------------------------------

    @_serialized
    def cancel_task(self, task_id: str) -> RunResult:
        """Cancel a task, its pending approvals, and any open run.

        Serialized on the run lock: an active run finishes first, so this can
        never interleave with a tool execution or an approval decision.
        """
        task = self.tasks.get(task_id)
        if task is None:
            raise ValueError(f"no such task: {task_id}")
        if task["status"] in ("completed", "failed", "cancelled"):
            return RunResult(
                "skipped", "", task_id, message="task is already terminal",
            )
        cancelled = 0
        for approval in self.approvals.pending(task_id):
            if self.approvals.cancel(
                approval["id"], reason="task cancelled by user"
            ):
                cancelled += 1
        for run in rows_to_dicts(self.db.query(
            "SELECT * FROM runs WHERE task_id=? AND status IN"
            " ('running','interrupted','waiting_user','waiting_approval')",
            (task_id,),
        )):
            self._finish_run(
                run["id"], "failed", "cancelled by user",
                error="task cancelled by user",
            )
        self.tasks.set_status(task_id, "cancelled", reason="cancelled by user")
        # Child work belongs to this task; leaving a paused child behind would
        # make the cancelled parent look unfinished and could be resumed.
        for child in self.tasks.children(task_id):
            if (child.get("role") or "user") != "subagent":
                continue
            if child["status"] not in ("completed", "failed", "cancelled"):
                self.cancel_task(child["id"])
        self.events.record(
            "task.cancelled", task_id=task_id,
            data={"approvals_cancelled": cancelled},
        )
        return RunResult(
            "cancelled", "", task_id,
            message="Task cancelled; pending approvals were cancelled too.",
        )

    @_serialized
    def resume_interrupted_run(self, run_id: str) -> RunResult:
        run = self._run_row(run_id)
        if run is None or run["status"] != "interrupted":
            task_id = run["task_id"] if run else ""
            return RunResult("skipped", run_id, task_id)
        task = self.tasks.get(run["task_id"])
        if task is None or task["status"] in ("completed", "failed", "cancelled"):
            # Never revive work the user (or a terminal outcome) ended.
            self._finish_run(
                run["id"], "failed", "not resumed",
                error=f"task is {task['status'] if task else 'missing'}",
            )
            return RunResult(
                "skipped", run["id"], run["task_id"],
                message="interrupted run belongs to a terminal task",
            )
        self._save_run(run["id"], status="running", ended_at=None)
        self.tasks.set_status(task["id"], "active", reason="recovered")
        self.events.record(
            "run.resumed", task_id=task["id"], run_id=run["id"],
            data={"reason": "interrupted run recovered"},
        )
        run = self._run_row(run["id"])
        return self._loop(run, {"kind": "resume"})

    @_serialized
    def retry_paused_task(self, task: dict[str, Any]) -> RunResult:
        """Auto-retry a task paused by a transient model failure. The run's
        transcript is intact, so completed actions are never repeated."""
        current = self.tasks.get(task["id"])
        if current is None or current["status"] not in ("paused", "failed"):
            return RunResult(
                "skipped", "", task["id"],
                message="only paused or failed tasks can be retried",
            )
        task = current
        run = self._latest_run(task["id"])
        if run is None:
            return self.start_run(task, {"kind": "resume"})
        if run["status"] not in ("failed", "limit_reached", "interrupted"):
            return RunResult("skipped", run["id"], task["id"])
        self._append_transcript(
            run["id"],
            {"role": "user", "content": (
                "[system] Retrying this task after a transient failure. "
                "Continue from where the transcript shows the work stopped; "
                "do not repeat actions that already have results."
            )},
        )
        self._save_run(
            run["id"], status="running", ended_at=None, error=None,
            decision=None,
        )
        self.tasks.set_status(task["id"], "active", reason="auto retry")
        self.events.record(
            "run.resumed", task_id=task["id"], run_id=run["id"],
            data={"reason": "auto retry after model failure"},
        )
        run = self._run_row(run["id"])
        return self._loop(run, {"kind": "resume"})

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def _loop(self, run: dict[str, Any], trigger: dict[str, Any]) -> RunResult:
        started_monotonic = time.monotonic()
        run_id = run["id"]
        task = self.tasks.get(run["task_id"])
        max_iterations, max_tool_calls, max_seconds = self._limits_for(task)
        self.tasks.clear_retry(task["id"], reset_count=False)
        # One controlled recovery is allowed for an over-long prompt; the
        # model client owns empty-reply recovery. Nothing else is retried here.
        context_retry_used = False

        while True:
            run = self._run_row(run_id)
            task = self.tasks.get(run["task_id"])
            iterations = int(run["iterations"])
            tool_calls = int(run["tool_calls"])

            # --- limits -------------------------------------------------
            if iterations >= max_iterations:
                return self._limit_reached(
                    run, task, f"iteration limit ({max_iterations})"
                )
            if tool_calls >= max_tool_calls:
                return self._limit_reached(
                    run, task, f"tool-call limit ({max_tool_calls})"
                )
            if time.monotonic() - started_monotonic > max_seconds:
                return self._limit_reached(
                    run, task, f"time limit ({max_seconds:.0f}s)"
                )

            # --- model call --------------------------------------------
            transcript = self._transcript(run)
            tools_schema = self._run_schemas(run, task)
            prompt, repairs, repaired = self._prepare_prompt(
                transcript, tools_schema
            )
            if repairs:
                self._save_run(run_id, transcript=util.dumps(repaired))
                self.events.record(
                    "transcript.repaired", level="warn", task_id=task["id"],
                    run_id=run_id, data={"repairs": repairs[:8]},
                )
            try:
                reply = self.model.chat(prompt, tools_schema)
            except ModelContextError as exc:
                if context_retry_used:
                    return self._model_failed(
                        run, task,
                        f"prompt still exceeds the model context window: {exc}",
                        retryable=False,
                    )
                context_retry_used = True
                tighter = max(1000, self._prompt_budget_tokens() // 2)
                prompt, extra = transcript_mod.compact(
                    prompt, tighter, tools_schema
                )
                self.events.record(
                    "model.context_retry", level="warn", task_id=task["id"],
                    run_id=run_id,
                    data={"budget_tokens": tighter, "repairs": extra[:8]},
                )
                try:
                    reply = self.model.chat(prompt, tools_schema)
                except (ModelUnavailable, ModelResponseError) as retry_exc:
                    return self._model_failed(
                        run, task, str(retry_exc),
                        retryable=isinstance(retry_exc, ModelUnavailable),
                    )
            except ModelEmptyReply as exc:
                # The client already made its one bounded recovery attempt.
                return self._model_failed(run, task, str(exc), retryable=False)
            except ModelUnavailable as exc:
                return self._model_failed(run, task, str(exc), retryable=True)
            except ModelResponseError as exc:
                return self._model_failed(
                    run, task, f"model protocol error: {exc}", retryable=False
                )
            self.events.record(
                "model.call", task_id=task["id"], run_id=run_id,
                data={
                    "latency_ms": reply.latency_ms,
                    "prompt_tokens": reply.prompt_tokens,
                    "completion_tokens": reply.completion_tokens,
                    "tool_calls": [c.name for c in reply.tool_calls],
                },
            )
            self._save_run(run_id, iterations=iterations + 1)

            # --- tool calls ---------------------------------------------
            if reply.tool_calls:
                self._append_transcript(
                    run_id,
                    {
                        "role": "assistant",
                        "content": reply.content or "",
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": call.raw_arguments,
                                },
                            }
                            for call in reply.tool_calls
                        ],
                    },
                )
                for index, call in enumerate(reply.tool_calls):
                    if index > 0:
                        # One action at a time keeps approvals and recovery
                        # exact; the rest of the batch is honestly deferred.
                        self._append_tool_result(
                            run_id, call.id,
                            {"status": "deferred",
                             "error": "only the first proposed action of a "
                                      "batch is executed; propose it again "
                                      "if it is still needed"},
                        )
                        continue
                    outcome = self._handle_tool_call(run, task, call)
                    if outcome.get("status") == "awaiting_approval":
                        return RunResult(
                            "waiting_approval", run_id, task["id"],
                            message=outcome.get("message", ""),
                            approval_id=outcome.get("approval_id"),
                        )
                    self._append_tool_result(
                        run_id, call.id, outcome,
                        outcome.get("approval_id"),
                    )
                    self._save_run(run_id, tool_calls=tool_calls + 1)
                    if (
                        outcome.get("status") == "blocked"
                        and (task.get("role") or "user") == "subagent"
                    ):
                        reason = str(
                            outcome.get("error") or "sub-agent action blocked"
                        )
                        meta = self.tasks.meta(task["id"])
                        meta["blocked_reason"] = util.clip(reason, 600)
                        self.tasks.set_meta(task["id"], meta)
                        self._finish_run(
                            run_id, "failed", f"blocked: {util.clip(reason, 200)}"
                        )
                        self.tasks.set_status(
                            task["id"], "failed",
                            reason=f"blocked: {util.clip(reason, 200)}",
                        )
                        return RunResult(
                            "blocked", run_id, task["id"],
                            message=reason, detail=reason,
                        )
                continue

            # --- final reply --------------------------------------------
            content = (reply.content or "").strip()
            if not content and not reply.tool_calls:
                # The client converts empty wire replies into ModelEmptyReply;
                # a test/alternate model can still hand back an empty string.
                # One bounded recovery already happened, so stop and let the
                # user decide rather than looping on identical requests.
                return self._model_failed(
                    run, task, "empty final reply after one recovery attempt",
                    retryable=False,
                )
            asks_user = ASK_USER_TAG in content
            content = content.replace(ASK_USER_TAG, "").strip()
            self._append_transcript(
                run_id, {"role": "assistant", "content": content}
            )
            note_id = None
            if content:
                note_id = self.messages.record(
                    "assistant", content, source="agent", task_id=task["id"],
                    run_id=run_id, domain_id=task.get("domain_id"),
                    visible=self._visible(task),
                )
            live = self.app.approvals.live_for_run(run_id)
            if live is not None:
                return self._park_for_approval(
                    run, task, live, message=content,
                    assistant_message_id=note_id,
                    note=(f"An approval for **{live['tool']}** is still "
                          "pending. Approve or reject it to continue; this "
                          "task was not completed."),
                )
            if asks_user:
                self._finish_run(
                    run_id, "waiting_user", "asked the user"
                )
                self.tasks.set_status(
                    task["id"], "waiting_user", reason="waiting for user input"
                )
                return RunResult(
                    "waiting_user", run_id, task["id"], message=content,
                    assistant_message_id=note_id,
                )
            self._finish_run(run_id, "completed", "final response delivered")
            self.tasks.set_status(task["id"], "completed", reason="done")
            return RunResult(
                "completed", run_id, task["id"], message=content,
                assistant_message_id=note_id,
            )

    # ------------------------------------------------------------------
    # Tool-call handling
    # ------------------------------------------------------------------

    def _handle_tool_call(
        self, run: dict[str, Any], task: dict[str, Any], call: ToolCall
    ) -> dict[str, Any]:
        run_id, task_id = run["id"], task["id"]
        role = task.get("role") or "user"
        # A sub-agent asking for a confirm-risk or parent-only tool gets an
        # explicit blocked result for its parent instead of an approval.
        known = self.tools.resolve_optional(call.name)
        restriction = (
            self.tools.role_restriction(known, role) if known else None
        )
        if restriction is not None:
            self.tools.record_rejected(
                call, run_id=run_id, task_id=task_id, error=restriction
            )
            return {"status": "blocked", "error": restriction}
        # Visibility is enforced here as well as in the prompt: a call by name
        # for a tool outside this run's snapshot is denied, not executed.
        allowed = self._allowed_tool_names(run_id)
        try:
            spec = self.tools.resolve(call.name, allowed=allowed, role=role)
        except Exception as exc:
            self.tools.record_rejected(
                call, run_id=run_id, task_id=task_id, error=str(exc)
            )
            return {"status": "denied", "error": str(exc)}

        # create_domain is a virtual action: if the domain already exists, or
        # an identical proposal is already pending, do not ask again.
        if call.name == "create_domain":
            name = str((call.arguments or {}).get("name") or "").strip()
            existing = self.domains.by_name(name) if name else None
            if existing is not None:
                return {
                    "status": "ok",
                    "result": {
                        "already_exists": existing["name"],
                        "domain_id": existing["id"],
                        "note": "the domain already exists; nothing was created",
                    },
                }
            for approval in self.approvals.pending():
                if (approval.get("meta") or {}).get("action") != "create_domain":
                    continue
                pending_name = str(
                    (approval.get("arguments") or {}).get("name") or ""
                ).strip().lower()
                if name and pending_name == name.lower():
                    return {
                        "status": "denied",
                        "error": (
                            f"A create_domain approval for '{name}' is already "
                            f"pending (approval {approval['id']}). Wait for "
                            "that decision; do not propose it again."
                        ),
                    }

        # Skill writes are approval-gated and deduped by skill name. A write in
        # the same run may refine the request that is already pending; any
        # other run must wait for that decision.
        if call.name in SKILL_WRITE_TOOLS:
            name = str((call.arguments or {}).get("name") or "").strip()
            for approval in self.approvals.pending():
                if approval.get("tool") not in SKILL_WRITE_TOOLS:
                    continue
                pending_name = str(
                    (approval.get("arguments") or {}).get("name") or ""
                ).strip().lower()
                if not name or pending_name != name.lower():
                    continue
                if approval.get("run_id") == run_id:
                    break
                return {
                    "status": "denied",
                    "error": (
                        f"A skill change for '{name}' is already pending "
                        f"({approval.get('tool')}, approval "
                        f"{approval['id']}). Wait for that decision; do "
                        "not propose it again."
                    ),
                }

        # Trust/taint policy: once this run has consumed untrusted content, a
        # side-effecting tool is escalated to confirm even when it is
        # configured low-risk or auto-approved. Read-only tools stay low.
        taint = self._taint_sources(run_id)
        if call.name in SKILL_WRITE_TOOLS and taint:
            origin = str(taint[0].get("origin") or "untrusted content")
            self.events.record(
                "skill.refused", level="warn", task_id=task_id, run_id=run_id,
                source="runtime",
                data={
                    "reason": "untrusted provenance",
                    "origin": util.clip(origin, 300),
                    "tool": call.name,
                    "name": str(
                        (call.arguments or {}).get("name") or ""
                    ).strip()[:120],
                },
            )
            return {
                "status": "denied",
                "error": (
                    "This run consumed untrusted content from "
                    f"{util.clip(origin, 160)}, so it cannot change a skill. "
                    "Skills are built only from trusted work."
                ),
            }
        tainted_side_effect = bool(
            taint and spec.side_effects and not spec.read_only
        )
        escalated = tainted_side_effect and spec.risk != "confirm"
        effective_risk = "confirm" if escalated else spec.risk
        taint_origin = str(taint[0].get("origin") or "") if taint else ""
        if escalated:
            self.events.record(
                "trust.escalated", task_id=task_id, run_id=run_id,
                data={
                    "tool": call.name,
                    "module": spec.module,
                    "from_risk": spec.risk,
                    "origin": taint_origin,
                },
            )

        if effective_risk == "confirm":
            pending = self.approvals.pending_for_run(run_id)
            pending_name = str(
                ((pending or {}).get("arguments") or {}).get("name") or ""
            ).strip().lower()
            call_name = str(
                (call.arguments or {}).get("name") or ""
            ).strip().lower()
            refining_skill = (
                pending is not None
                and call.name in SKILL_WRITE_TOOLS
                and pending.get("tool") in SKILL_WRITE_TOOLS
                and bool(call_name)
                and call_name == pending_name
            )
            if pending is not None and not refining_skill:
                # A non-skill action, or a different skill, is already
                # awaiting a decision; do not stack a second one.
                return {
                    "status": "denied",
                    "error": "another action is already awaiting approval "
                             f"({pending['tool']}); wait for that decision.",
                }
            try:
                arguments = self.tools.validate(
                    call, allowed=allowed, role=role
                )
            except Exception as exc:
                return {"status": "denied", "error": str(exc)}
            if call.name in SKILL_WRITE_TOOLS:
                skills = self.app.modules.skills
                if not skills.enabled:
                    return {
                        "status": "denied",
                        "error": "skills are disabled in configuration",
                    }
                try:
                    if refining_skill:
                        # Build on the latest pending request, not the older
                        # skill row, so successive refinements compose.
                        base = dict(pending.get("arguments") or {})
                        existing = skills.find_by_name(
                            str(base.get("name") or "")
                        )
                        if (
                            existing is not None
                            and not skills.is_agent_owned(existing)
                        ):
                            return {
                                "status": "denied",
                                "error": skills.ownership_error(existing),
                            }
                        arguments = skills.merge_fields(
                            base,
                            description=arguments.get("description"),
                            preconditions=arguments.get("preconditions"),
                            domains=arguments.get("domains"),
                            body=arguments.get("body"),
                        )
                    elif call.name == "edit_skill":
                        _, arguments = skills.edit_plan(
                            name=str(arguments.get("name") or ""),
                            description=arguments.get("description"),
                            preconditions=arguments.get("preconditions"),
                            domains=arguments.get("domains"),
                            body=arguments.get("body"),
                        )
                    else:
                        arguments = skills.validate_fields(
                            name=str(arguments.get("name") or ""),
                            description=str(
                                arguments.get("description") or ""
                            ),
                            preconditions=arguments.get("preconditions"),
                            domains=arguments.get("domains"),
                            body=str(arguments.get("body") or ""),
                        )
                        existing = skills.find_by_name(arguments["name"])
                        if existing is not None and not skills.is_agent_owned(
                            existing
                        ):
                            return {
                                "status": "denied",
                                "error": skills.ownership_error(existing),
                            }
                except ValueError as exc:
                    return {"status": "denied", "error": str(exc)}
            if refining_skill:
                return self._refine_pending_skill_approval(
                    run_id, task, pending, call, arguments,
                )
            action = "create_domain" if call.name == "create_domain" else "tool"
            reason = (
                f"untrusted content from {taint_origin} requires approval "
                f"before {call.name} can change state"
                if tainted_side_effect else f"confirm-risk tool: {call.name}"
            )
            approval = self.approvals.create(
                task_id=task_id, run_id=run_id, action=action,
                tool=call.name, arguments=arguments,
                risk=effective_risk, tool_call_id=call.id,
                reason=reason,
            )
            execution_id = util.new_id("ex")
            self.db.execute(
                "INSERT INTO tool_executions(id, run_id, task_id, tool,"
                " tool_call_id, arguments, status, started_at, approval_id)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    execution_id, run_id, task_id, call.name, call.id,
                    call.raw_arguments, "awaiting_approval", util.iso(),
                    approval["id"],
                ),
            )
            # Keep the transcript protocol-valid while the decision is
            # pending: a tool call must always have a result. The real result
            # replaces this placeholder when the approval is executed.
            self._append_tool_result(run_id, call.id, {
                "status": "awaiting_approval",
                "approval_id": approval["id"],
                "note": "waiting for the user's decision; no result yet",
            })
            self._finish_run(run_id, "waiting_approval", "action needs approval")
            self.tasks.set_status(
                task_id, "waiting_approval",
                reason=f"awaiting approval for {call.name}",
            )
            provenance = (
                f" This run contains untrusted content from **{taint_origin}**, "
                "so a side-effecting action needs your explicit approval."
                if tainted_side_effect else ""
            )
            if call.name in SKILL_WRITE_TOOLS:
                if call.name == "edit_skill":
                    lead = (
                        "I want to refine an **agent-created skill**. Review "
                        "the full bounded body below; approval writes a new "
                        "draft version and never activates it."
                    )
                else:
                    lead = (
                        "I want to save a **draft skill**. Review the full "
                        "bounded body below; approval writes a draft and "
                        "never activates it."
                    )
                note = (
                    f"{lead}\n\n"
                    f"{self._skill_proposal_detail(arguments)}\n\n"
                    "Reply approve/reject, or use the Approvals panel."
                    f"{provenance}"
                )
            else:
                note = (
                    f"I want to run **{call.name}** with "
                    f"{util.clip(util.dumps(arguments), 400)}.{provenance} "
                    "This action needs your approval (reply approve/reject, "
                    "or use the Approvals panel)."
                )
            self.messages.record(
                "note", note, source="system", task_id=task_id, run_id=run_id,
                domain_id=task.get("domain_id"), visible=self._visible(task),
            )
            return {
                "status": "awaiting_approval",
                "approval_id": approval["id"],
                "message": note,
            }

        ctx = self.app.tool_context(run_id, task_id, task.get("domain_id"))
        outcome = self.tools.execute(
            call, ctx, run_id=run_id, task_id=task_id,
            allowed_names=allowed, role=role,
        )
        if outcome.get("status") == "succeeded" and spec.untrusted_output:
            self._mark_tainted(
                run_id, call.name, spec.module,
                self._taint_origin(outcome.get("result")),
            )
        return outcome

    def _refine_pending_skill_approval(
        self,
        run_id: str,
        task: dict[str, Any],
        pending: dict[str, Any],
        call: ToolCall,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Fold a same-run skill write into the approval already pending.

        The user keeps exactly one decision card: the approval's arguments
        and the awaiting execution row are updated in place, so approving or
        rejecting applies to the latest version rather than a stale one. No
        skill is written and no second approval is created.
        """
        tool = str(pending.get("tool"))
        reason = f"confirm-risk tool: {tool} (refined before approval)"
        try:
            approval = self.approvals.update_pending(
                pending["id"], arguments=arguments, reason=reason,
            )
        except ApprovalError as exc:
            return {"status": "denied", "error": str(exc)}
        execution_id = self._awaiting_execution_id(approval["id"])
        if execution_id is not None:
            self.db.execute(
                "UPDATE tool_executions SET arguments=? WHERE id=?"
                " AND status='awaiting_approval'",
                (util.dumps(arguments), execution_id),
            )
        self.events.record(
            "approval.refined", task_id=task["id"], run_id=run_id,
            data={
                "approval_id": approval["id"],
                "tool": tool,
                "name": arguments.get("name"),
                "via": call.name,
            },
        )
        note = (
            f"Refined the pending **{tool}** request for "
            f"'{arguments.get('name')}'. The approval now shows this version; "
            "no skill was written and no new approval was created. Approve "
            "or reject it when ready.\n\n"
            f"{self._skill_proposal_detail(arguments)}"
        )
        self.messages.record(
            "note", note, source="system", task_id=task["id"], run_id=run_id,
            domain_id=task.get("domain_id"), visible=self._visible(task),
        )
        return {
            "status": "ok",
            "result": {
                "approval_id": approval["id"],
                "tool": tool,
                "name": arguments.get("name"),
                "note": "the pending approval was updated in place; no skill "
                        "was written and no new approval was created",
            },
        }

    def _skill_proposal_detail(self, arguments: dict[str, Any]) -> str:
        """Bounded, readable approval content for a skill write call."""
        def listed(key: str) -> str:
            value = arguments.get(key)
            if isinstance(value, (list, tuple)):
                items = [str(item).strip() for item in value if str(item).strip()]
            elif value:
                items = [str(value).strip()]
            else:
                items = []
            return ", ".join(items) or "(none)"

        name = util.clip(str(arguments.get("name") or "").strip(), 120)
        description = util.clip(
            str(arguments.get("description") or "").strip(), 500
        )
        limit = max(1, int(self.config.get("skills.max_body_chars", 6000)))
        body = util.clip(str(arguments.get("body") or ""), limit)
        return (
            f"- name: {name or '(missing)'}\n"
            f"- description: {description or '(none)'}\n"
            f"- preconditions: {listed('preconditions')}\n"
            f"- domains: {listed('domains')}\n\n"
            f"Body (limit {limit} chars):\n{body}"
        )

    def _append_tool_result(
        self, run_id: str, call_id: str, outcome: dict[str, Any],
        approval_id: str | None = None,
    ) -> None:
        """Record one tool result, replacing an awaiting-approval placeholder.

        Exactly one result per tool call keeps the transcript valid for strict
        chat templates; a later real result must not be appended beside the
        placeholder.
        """
        body = outcome.get("result", outcome)
        text = util.clip(util.dumps(body), int(
            self.config.get("limits.max_tool_result_chars", 6000)
        ))
        run = self._run_row(run_id)
        transcript = self._transcript(run)
        for index in range(len(transcript) - 1, -1, -1):
            message = transcript[index]
            if (
                message.get("role") != "tool"
                or message.get("tool_call_id") != call_id
            ):
                continue
            existing = util.loads(message.get("content") or "", {})
            placeholder = (
                isinstance(existing, dict)
                and existing.get("status") in ("awaiting_approval", "unknown")
            )
            if placeholder:
                transcript[index] = {
                    "role": "tool", "tool_call_id": call_id, "content": text,
                }
                self._save_run(run_id, transcript=util.dumps(transcript))
            return
        transcript.append({
            "role": "tool", "tool_call_id": call_id, "content": text,
        })
        self._save_run(run_id, transcript=util.dumps(transcript))

    # ------------------------------------------------------------------
    # Terminal states
    # ------------------------------------------------------------------

    def _park_for_approval(
        self,
        run: dict[str, Any],
        task: dict[str, Any],
        approval: dict[str, Any],
        *,
        message: str = "",
        assistant_message_id: int | None = None,
        note: str,
    ) -> RunResult:
        """Keep a run alive when a model turn ends with a live approval.

        Completing or failing the task here would orphan the authorization:
        the action could never execute and the user would see a stale
        approval. Parking preserves the exact run/approval binding.
        """
        self._finish_run(
            run["id"], "waiting_approval", "turn ended with approval pending"
        )
        self.tasks.set_status(
            task["id"], "waiting_approval",
            reason=f"waiting for approval of {approval['tool']}",
        )
        self.messages.record(
            "note", note, source="system", task_id=task["id"],
            run_id=run["id"], domain_id=task.get("domain_id"),
            visible=self._visible(task),
        )
        return RunResult(
            "waiting_approval", run["id"], task["id"], message=message,
            assistant_message_id=assistant_message_id,
            approval_id=approval["id"],
        )

    def _finish_run(
        self, run_id: str, status: str, decision: str, error: str | None = None
    ) -> None:
        self._save_run(
            run_id, status=status, ended_at=util.iso(), decision=decision,
            error=error,
        )
        self.events.record(
            "run.status", run_id=run_id,
            data={"status": status, "decision": decision},
        )

    def _limit_reached(
        self, run: dict[str, Any], task: dict[str, Any], reason: str
    ) -> RunResult:
        live = self.app.approvals.live_for_run(run["id"])
        if live is not None:
            return self._park_for_approval(
                run, task, live,
                note=(f"This run reached its {reason}, but an approval for "
                      f"**{live['tool']}** is still pending. Approve or "
                      "reject it to continue."),
            )
        self._finish_run(run["id"], "limit_reached", reason)
        self.tasks.set_status(task["id"], "paused", reason=reason)
        self.events.record(
            "limit.reached", level="warn", task_id=task["id"], run_id=run["id"],
            data={"reason": reason},
        )
        note_id = self.messages.record(
            "note",
            f"Paused: this run reached its {reason}. The work is saved and "
            "can be resumed by sending a message like 'continue'.",
            source="system", task_id=task["id"], run_id=run["id"],
            domain_id=task.get("domain_id"), visible=self._visible(task),
        )
        return RunResult(
            "limit_reached", run["id"], task["id"], assistant_message_id=note_id,
            detail=reason,
        )

    def _model_failed(
        self, run: dict[str, Any], task: dict[str, Any], error: str,
        *, retryable: bool = True,
    ) -> RunResult:
        live = self.app.approvals.live_for_run(run["id"])
        if live is not None:
            return self._park_for_approval(
                run, task, live,
                message=(
                    "I could not reach the model endpoint, and an approval "
                    f"for **{live['tool']}** is still pending."
                ),
                note=(
                    "The model endpoint was unavailable while an approval "
                    f"was pending ({util.clip(error, 200)}). The approval "
                    "was kept; approve or reject it to continue."
                ),
            )
        category = "model unavailable" if retryable else "model protocol error"
        self._finish_run(run["id"], "failed", category, error=error)
        if retryable:
            reason = f"model failure: {util.clip(error, 200)}"
            self.tasks.set_retry(task["id"], None)
            note_text = (
                "I could not reach the model endpoint; the work is paused and "
                "will be retried automatically with backoff. "
                f"Error: {util.clip(error, 200)}"
            )
        else:
            reason = f"model protocol failure: {util.clip(error, 200)}"
            self.tasks.set_retry(task["id"], None)
            note_text = (
                "The model returned a reply I cannot use safely, so the work "
                "is paused instead of being retried in a loop. Send "
                "`continue` (or a new instruction) when you want to try "
                f"again. Error: {util.clip(error, 200)}"
            )
        self.tasks.set_status(task["id"], "paused", reason=reason)
        self.events.record(
            "run.failed", level="error", task_id=task["id"], run_id=run["id"],
            data={"error": util.clip(error, 300), "retryable": retryable},
        )
        note_id = self.messages.record(
            "note", note_text,
            source="system", task_id=task["id"], run_id=run["id"],
            domain_id=task.get("domain_id"), visible=self._visible(task),
        )
        return RunResult(
            "failed", run["id"], task["id"], assistant_message_id=note_id,
            detail=util.clip(error, 300),
        )

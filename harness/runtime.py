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
import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any

from . import util
from . import transcript as transcript_mod
from .approvals import ApprovalError
from .config import (
    Config,
    ROUTING_MARGIN_FALLBACK,
    ROUTING_THRESHOLD_FALLBACK,
)
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
from .providers import configured_provider_id
from .store import Domains, Events, Messages, Tasks
from .routing import RoutingService
from .tools import ToolDenied, ToolError, ToolSpec, validate_arguments


SKILL_WRITE_TOOLS = frozenset({"propose_skill", "edit_skill"})
DOMAIN_WRITE_TOOLS = frozenset({"edit_domain"})
UNTRUSTED_ROUTING_SOURCES = frozenset({
    "inbox", "automation", "schedule", "heartbeat",
})
MODEL_ROUTING_MAX_TOKENS = 16

_REVISION_CUES = (
    "instead", "change", "make it", "change it", "use", "not that",
    "rather", "shorter", "longer", "different", "rename", "add", "remove",
    "rewrite", "try", "but", "don't", "do not", "actually",
)
# Match whole words: a substring test read "exchange rates" as a change
# request and swallowed a new topic while an approval was pending.
_REVISION_CUE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(cue) for cue in _REVISION_CUES) + r")\b"
)


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
        self.router = RoutingService(app)
        self._lock = threading.RLock()
        # One shared classifier worker bounds abandoned timed-out calls: a
        # hung endpoint can occupy at most this one thread, and later calls
        # queue behind it instead of creating unbounded executors.
        self._classifier_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="domain-classifier"
        )

    def close(self) -> None:
        """Stop background helpers owned by the runtime."""
        try:
            self._classifier_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

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
                self.config.get("context.reserved_output_tokens", 4096)
            )
            return max(1000, window - reserved)
        return int(self.config.get("context.total_budget_tokens", 20000))

    def _schema_budget(self) -> int:
        return max(0, int(
            self.config.get("modules.tool_schema_budget_tokens", 4096)
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
        """The durable part of a capability selection.

        ``specs`` freezes each selected tool's schema and policy metadata.
        Later model calls and permission decisions in this run read those
        values instead of the live registry, so a reload cannot change them
        mid-run.
        """
        snapshot = {
            key: selection[key]
            for key in (
                "domain_id", "role", "names", "modules", "exclusions",
                "tokens", "budget_tokens",
            )
        }
        snapshot["specs"] = list(selection.get("specs") or [])
        return snapshot

    def _model_stamp(self) -> dict[str, str]:
        """The model/provider a run records and keeps for its own turns."""
        return {
            "model": str(self.config.get("model.name", "")),
            "provider": configured_provider_id(self.config),
        }

    def _run_meta(self, run_id: str) -> dict[str, Any]:
        run = self._run_row(run_id)
        meta = util.loads(run.get("meta") or "", {}) if run else {}
        return meta if isinstance(meta, dict) else {}

    def _save_meta(self, run_id: str, meta: dict[str, Any]) -> None:
        self._save_run(run_id, meta=util.dumps(meta))

    @staticmethod
    def _args_hash(arguments: dict[str, Any]) -> str:
        """Hash canonical JSON arguments for exact denial matching."""
        canonical = util.dumps(arguments).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _denied_actions(self, run_id: str) -> list[dict[str, Any]]:
        denied = self._run_meta(run_id).get("denied_actions")
        if not isinstance(denied, list):
            return []
        return [entry for entry in denied if isinstance(entry, dict)]

    def _remember_denied_action(
        self, run_id: str, tool: str, arguments: dict[str, Any]
    ) -> None:
        """Remember a rejected action in this run, newest entries last."""
        meta = self._run_meta(run_id)
        denied = meta.get("denied_actions")
        if not isinstance(denied, list):
            denied = []
        denied.append({
            "tool": str(tool),
            "args_hash": self._args_hash(arguments),
            "at": util.iso(),
        })
        meta["denied_actions"] = denied[-20:]
        self._save_run(run_id, meta=util.dumps(meta))

    def _clear_denied_actions(self, run_id: str) -> None:
        """A new user turn permits asking again in the same run."""
        meta = self._run_meta(run_id)
        if "denied_actions" not in meta:
            return
        meta.pop("denied_actions", None)
        self._save_run(run_id, meta=util.dumps(meta))

    def _toolset(self, run_id: str) -> dict[str, Any]:
        snapshot = self._run_meta(run_id).get("toolset")
        return snapshot if isinstance(snapshot, dict) else {}

    def _frozen_spec(self, run_id: str, name: str) -> ToolSpec | None:
        """The run's frozen definition for one tool, or ``None``.

        The handler still comes from the live registry; a removed or disabled
        tool fails closed here (returns ``None``) instead of being resurrected
        from the snapshot.
        """
        try:
            return self.tools.resolve_frozen(
                name, snapshot=self._toolset(run_id)
            )
        except (ToolError, ToolDenied):
            return None

    def approval_tool_spec(
        self, approval: dict[str, Any]
    ) -> ToolSpec | None:
        """The tool definition a pending approval was created against.

        Approval summaries, the schema shown in the edit form, and edited
        arguments all read the run's frozen definition, so a module reload
        between the request and the decision cannot change what the user is
        approving. A legacy name-only snapshot falls back to the live
        definition; a tool removed since the run started returns ``None``.
        """
        return self._frozen_spec(
            str(approval.get("run_id") or ""),
            str(approval.get("tool") or ""),
        )

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
            return self.tools.schemas_for_snapshot(snapshot)
        selection = self._select_tools(domain_id, role)
        self._save_toolset(run["id"], selection)
        return selection["schemas"]

    # -- trust / taint -----------------------------------------------------

    def _taint_sources(self, run_id: str) -> list[dict[str, Any]]:
        sources = self._run_meta(run_id).get("tainted_sources")
        return sources if isinstance(sources, list) else []

    def _first_taint_origin(self, run_id: str) -> str:
        """The first recorded untrusted origin for the run, '' when clean."""
        taint = self._taint_sources(run_id)
        return str(taint[0].get("origin") or "") if taint else ""

    def run_tainted(self, run_id: str) -> bool:
        """Whether the run has consumed untrusted content."""
        return bool(self._taint_sources(run_id))

    def _taint_origin(self, result: Any, tool: str = "") -> str:
        """Name the external origin an approval prompt must identify.

        Only generic keys are inspected: a server is free to return a URL or a
        list of source URLs; anything else falls back to its own text.
        """
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
                return f"{tool or 'tool'} results for '{result['query']}'"
            content = result.get("content")
            if isinstance(content, str) and content.strip():
                return util.clip(content.strip(), 300)
        return util.clip(str(result or ""), 300)

    def _dynamic_taint_sources(self, result: Any) -> list[dict[str, str]]:
        """Validated untrusted provenance carried by a builtin tool result."""
        if not isinstance(result, dict):
            return []
        raw = result.get("untrusted_sources")
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        for item in raw[:20]:
            if not isinstance(item, dict):
                continue
            origin = str(item.get("origin") or "").strip()
            if not origin:
                continue
            out.append({
                "tool": str(item.get("tool") or "builtin")[:120],
                "module": str(item.get("module") or "builtin")[:120],
                "origin": util.clip(origin, 300),
                "at": str(item.get("at") or util.iso())[:40],
                "derived": bool(item.get("derived")),
            })
        return out

    def _record_result_taint(
        self, run_id: str, call_name: str, spec: ToolSpec, result: Any
    ) -> list[dict[str, str]]:
        """Apply static and result-carried trust metadata to one run."""
        sources: list[dict[str, str]] = []
        if spec.untrusted_output:
            sources.append({
                "tool": call_name,
                "module": spec.module,
                "origin": self._taint_origin(result, call_name),
                "at": util.iso(),
            })
        sources.extend(self._dynamic_taint_sources(result))
        for source in sources:
            self._mark_tainted(
                run_id, source["tool"], source["module"], source["origin"]
            )
        return sources

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
        *,
        run_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
        """Repair, compact, and reconcile actual model-visible context."""
        repaired, repairs = transcript_mod.repair(transcript)
        prompt, compacted = transcript_mod.compact(
            repaired, self._prompt_budget_tokens(), tools_schema
        )
        repairs = repairs + compacted
        if run_id:
            self._reconcile_prompt_context(run_id, prompt, repairs)
        return prompt, repairs, repaired

    def _reconcile_prompt_context(
        self,
        run_id: str,
        prompt: list[dict[str, Any]],
        repairs: list[str],
    ) -> None:
        """Make the source ledger and memory usage match the final payload."""
        run = self._run_row(run_id)
        if run is None:
            return
        sources = util.loads(run.get("context_sources") or "[]", [])
        if not isinstance(sources, list):
            sources = []
        system = str(prompt[0].get("content") or "") if prompt else ""
        access_ids: list[str] = []
        changed = False
        for source in sources:
            if not isinstance(source, dict):
                continue
            memory_id = str(source.get("memory_id") or "")
            if not memory_id and not source.get("history_marker"):
                continue
            included = self.context.source_visible(source, system)
            disposition = "included" if included else "excluded"
            if source.get("disposition") != disposition:
                source["disposition"] = disposition
                changed = True
                if not included:
                    source["reason"] = (
                        str(source.get("reason") or "memory candidate")
                        + "; excluded by transcript compaction"
                    )
            if included and memory_id:
                access_ids.append(memory_id)
        for source in sources:
            if not isinstance(source, dict):
                continue
            if (
                source.get("untrusted")
                and source.get("disposition") != "excluded"
                and source.get("seed_taint") is not False
            ):
                self._mark_tainted(
                    run_id,
                    str(source.get("tool") or "context"),
                    str(source.get("module") or "context"),
                    str(source.get("origin") or "untrusted context"),
                )
                # _mark_tainted owns the timestamp; attach the provenance hop
                # without changing the deduplication key or approval origin.
                meta = self._run_meta(run_id)
                entries = meta.get("tainted_sources") or []
                if entries:
                    entries[-1]["derived"] = bool(source.get("derived"))
                    self._save_meta(run_id, meta)
        if repairs:
            # Compaction is per-call, not a rewrite of the durable transcript;
            # retain its effect in the final-payload audit trail instead.
            sources.extend({
                "section": "transcript", "ref": None,
                "reason": f"final payload: {note}", "disposition": "transformed",
            } for note in repairs)
            changed = True
        if changed:
            self._save_run(run_id, context_sources=util.dumps(sources))
        if access_ids:
            self.memory.record_access(access_ids, run_id=run_id)


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
                target = self._revision_feedback_target(text, pending)
                if target is not None:
                    return self._revise_from_user_message(
                        text, source, target
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

    def _revision_feedback_target(
        self, text: str, pending: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Return the one pending approval targeted by revision feedback."""
        if len(pending) != 1:
            return None
        stripped = (text or "").strip()
        if not stripped or self._looks_like_question(stripped):
            return None
        if len(stripped) > 600 or len(stripped.split()) > 60:
            return None

        approval = pending[0]
        task = self.tasks.get(approval.get("task_id"))
        if task is None:
            return None
        domain_id = task.get("domain_id")
        candidate = self.tasks.find_resume_candidate(
            stripped, domain_id=domain_id
        )
        candidate_task = candidate.get("task")
        if (
            candidate_task is not None
            and candidate_task.get("id") != approval.get("task_id")
            and float(candidate.get("score") or 0.0) >= 2.0
        ):
            # A strong match for another task is a new topic, not feedback.
            return None

        lowered = " ".join(stripped.lower().split())
        if _REVISION_CUE_RE.search(lowered):
            return approval

        reply_words = set(util.keywords(stripped))
        if not reply_words:
            return None
        context = set(
            util.keywords(str(approval.get("tool") or "").replace("_", " "))
        )
        arguments = approval.get("arguments") or {}
        if isinstance(arguments, dict):
            for value in arguments.values():
                if isinstance(value, str):
                    context.update(util.keywords(value))
        context.update(util.keywords(str(approval.get("reason") or "")))
        context.update(util.keywords(str(task.get("objective") or "")))
        # One shared content word is not enough: "maybe we should discuss the
        # telescope later" mentions the action's subject but is not feedback.
        # A revision cue above remains sufficient; otherwise require at least
        # two context terms so ambiguous replies reach the ambiguity question.
        overlap = reply_words & context
        return approval if len(overlap) >= 2 else None

    def _revise_from_user_message(
        self, text: str, source: str, approval: dict[str, Any]
    ) -> RunResult:
        """Record free-text feedback, close the approval, and re-propose."""
        message_id = self.messages.record("user", text, source=source)
        self.db.execute(
            "UPDATE messages SET task_id=?, domain_id=? WHERE id=?",
            (
                approval["task_id"],
                self._task_domain(approval["task_id"]),
                message_id,
            ),
        )
        self._clear_denied_actions(approval["run_id"])
        revised = self.approvals.revise(
            approval["id"], feedback=text, decided_by=f"feedback:{source}"
        )
        return self.resume_after_revision(revised, text, source)

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
        task_meta: dict[str, Any] | None = None,
        trigger: dict[str, Any] | None = None,
    ) -> RunResult:
        # Route first: the routed domain is evidence for which open task this
        # message continues. Doing it the other way around let a resume
        # candidate from an unrelated domain win.
        if domain_override:
            domain_id = domain_override
            routing = {
                "domain_id": domain_id,
                "reason": "explicit domain from the user",
                "candidates": [],
                "breakdown": {},
                "matched": [],
                "hint": None,
                "model_choice": None,
                "model_route": "off",
            }
            self.events.record(
                "domain.routed", domain_id=domain_id,
                data={"reason": "explicit domain from the user",
                      "candidates": [], "model_choice": None,
                      "model_route": "off"},
            )
        else:
            source_name = str(source or "").strip().lower()
            untrusted_source = source_name in UNTRUSTED_ROUTING_SOURCES or any(
                source_name.startswith(f"{name}:")
                for name in UNTRUSTED_ROUTING_SOURCES
            )
            routing = self._route_domain(
                text,
                trusted=not untrusted_source,
                task_meta=task_meta,
                trigger=trigger,
                source=source,
            )
            domain_id = str(routing.get("domain_id") or "general")
        candidate = self.tasks.find_resume_candidate(text, domain_id=domain_id)

        if candidate.get("ambiguous"):
            # Competing candidates are materially ambiguous: create a new
            # task that asks the user which one to continue.
            task = self.tasks.create(
                objective=text, domain_id=domain_id,
                origin="user", origin_ref=str(message_id),
                meta={
                    "notes": "",
                    "ambiguous_candidates": candidate["ambiguous"],
                    "routing": self._routing_payload(routing),
                },
            )
            return self.start_run(
                task, {"kind": "user", "routing": routing}, message_id
            )

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
            origin_ref=str(message_id),
            meta={
                "notes": "",
                "routing": self._routing_payload(routing),
            },
        )
        return self.start_run(
            task, {"kind": "user", "routing": routing}, message_id
        )

    @staticmethod
    def _routing_payload(routing: dict[str, Any]) -> dict[str, Any]:
        """Keep only JSON-safe routing evidence in a new task's metadata."""
        candidates: list[dict[str, Any]] = []
        raw_candidates = routing.get("candidates")
        if isinstance(raw_candidates, list):
            for candidate in raw_candidates:
                if not isinstance(candidate, dict):
                    continue
                raw_breakdown = candidate.get("breakdown")
                breakdown = (
                    util.loads(util.dumps(raw_breakdown))
                    if isinstance(raw_breakdown, dict) else {}
                )
                candidate_matched = breakdown.get("matched")
                if not isinstance(candidate_matched, list):
                    candidate_matched = []
                try:
                    score = float(candidate.get("score") or 0.0)
                except (TypeError, ValueError):
                    score = 0.0
                candidates.append({
                    "domain_id": str(candidate.get("domain_id") or ""),
                    "name": str(candidate.get("name") or ""),
                    "score": score,
                    "breakdown": breakdown,
                    "matched": [str(term) for term in candidate_matched],
                })
        matched = routing.get("matched")
        hint = routing.get("hint")
        try:
            score = float(routing.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        model_choice = routing.get("model_choice")
        if not isinstance(model_choice, str) or not model_choice:
            model_choice = None
        return {
            "domain_id": str(routing.get("domain_id") or "general"),
            "score": score,
            "reason": str(routing.get("reason") or ""),
            "model_choice": model_choice,
            "model_route": str(routing.get("model_route") or "off"),
            "candidates": candidates,
            "matched": (
                [str(term) for term in matched]
                if isinstance(matched, list) else []
            ),
            "hint": (
                util.loads(util.dumps(hint))
                if isinstance(hint, dict) else None
            ),
            "strategy": str(routing.get("strategy") or "lexical"),
            "confidence": str(routing.get("confidence") or "low"),
            "latency_ms": routing.get("latency_ms"),
            "inputs": (
                util.loads(util.dumps(routing.get("inputs")))
                if isinstance(routing.get("inputs"), dict) else {}
            ),
            "fallback": routing.get("fallback"),
            "shadow": (
                util.loads(util.dumps(routing.get("shadow")))
                if isinstance(routing.get("shadow"), list) else []
            ),
        }

    def _routing_learned(self, text: str) -> dict[str, list[str]]:
        """Merge memory and skill affinity for deterministic routing."""
        merged: dict[str, list[str]] = {}
        sources = (
            self.memory.domain_affinity(text),
            self.app.modules.skills.domain_affinity(text),
        )
        for source in sources:
            for domain_id, terms in source.items():
                bucket = merged.setdefault(str(domain_id), [])
                for term in terms:
                    if term not in bucket:
                        bucket.append(term)
        return merged

    def _route_domain(
        self,
        text: str,
        trusted: bool = True,
        task_meta: dict[str, Any] | None = None,
        trigger: dict[str, Any] | None = None,
        source: str = "web",
    ) -> dict[str, Any]:
        """Route through the configured strategy, then a lexical tie-break.

        The model path is deliberately narrow: it runs only for trusted input
        when deterministic evidence is weak, it receives no tools, and its
        output is treated only as a domain label.  A timeout or any classifier
        failure leaves the deterministic General fallback in place.
        """
        router_result = self.router.route(
            text,
            source=source,
            trigger=trigger,
            task_meta=task_meta,
            trusted=trusted,
        )
        routing = router_result.as_routing()
        mode = str(
            self.config.get("domains.model_routing", "off") or "off"
        ).strip().lower()
        # The legacy optional tie-break only applies to the lexical control.
        # Semantic strategies return their own label/confidence.
        if router_result.strategy != "lexical":
            mode = "off"
        model_choice: str | None = None
        model_raw: str | None = None
        model_route = "off"
        active_domains = [
            domain for domain in self.domains.list_active()
            if str(domain.get("id") or "") != "general"
        ]
        tainted = self._routing_has_taint(task_meta, trigger)
        if (
            mode in ("shadow", "on")
            and trusted
            and not tainted
            and self._routing_evidence_is_weak(routing, active_domains)
        ):
            model_route = mode
            model_choice, model_raw = self._run_model_domain_classifier(
                text, self._classifier_domains(routing, active_domains)
            )
            if mode == "on":
                if model_choice and model_choice != "general":
                    routing["domain_id"] = model_choice
                    routing["reason"] = (
                        "model tie-break selected domain "
                        f"{model_choice!r} from weak deterministic evidence"
                    )
                elif model_choice == "general":
                    routing["domain_id"] = "general"
                    routing["reason"] = (
                        "model tie-break selected general from weak "
                        "deterministic evidence"
                    )
                else:
                    routing["domain_id"] = "general"
                    routing["reason"] = (
                        "model tie-break was invalid or unavailable; fell "
                        "back to general"
                    )

        routing["model_choice"] = model_choice
        routing["model_route"] = model_route
        data = {
            "reason": routing["reason"],
            "candidates": routing["candidates"],
            "breakdown": routing.get("breakdown") or {},
            "matched": routing.get("matched") or [],
            "hint": routing.get("hint"),
            "model_choice": model_choice,
            "model_route": model_route,
            "strategy": routing.get("strategy"),
            "chosen_domain": routing.get("domain_id"),
            "top3": (routing.get("candidates") or [])[:3],
            "confidence": routing.get("confidence"),
            "latency_ms": routing.get("latency_ms"),
            "inputs": routing.get("inputs") or {},
            "fallback": routing.get("fallback"),
            "shadow": routing.get("shadow") or [],
        }
        if model_raw is not None:
            data["model_raw"] = util.clip(model_raw, 120)
        self.events.record(
            "domain.routed",
            domain_id=routing["domain_id"],
            data=data,
        )
        return routing

    @staticmethod
    def _routing_has_taint(
        task_meta: dict[str, Any] | None,
        trigger: dict[str, Any] | None,
    ) -> bool:
        """Defensively refuse model routing for already-tainted work."""
        for payload in (task_meta, trigger):
            if not isinstance(payload, dict):
                continue
            if payload.get("tainted_sources"):
                return True
        return False

    def _routing_evidence_is_weak(
        self,
        routing: dict[str, Any],
        active_domains: list[dict[str, Any]],
    ) -> bool:
        """Whether deterministic routing fell back to General on weak evidence."""
        if str(routing.get("domain_id") or "general") != "general":
            return False
        if not active_domains:
            return False
        try:
            threshold = float(self.config.get(
                "domains.routing_threshold", ROUTING_THRESHOLD_FALLBACK
            ))
        except (TypeError, ValueError):
            threshold = float(ROUTING_THRESHOLD_FALLBACK)
        try:
            margin = float(self.config.get(
                "domains.ambiguity_margin", ROUTING_MARGIN_FALLBACK
            ))
        except (TypeError, ValueError):
            margin = float(ROUTING_MARGIN_FALLBACK)

        scores: list[float] = []
        candidates = routing.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                if str(candidate.get("domain_id") or "") == "general":
                    continue
                try:
                    scores.append(float(candidate.get("score") or 0.0))
                except (TypeError, ValueError):
                    scores.append(0.0)
        scores.sort(reverse=True)
        best_score = scores[0] if scores else 0.0
        reason = str(routing.get("reason") or "").lower()
        ambiguous = "ambiguous domain evidence" in reason
        if len(scores) >= 2:
            ambiguous = ambiguous or (scores[0] - scores[1] < margin)
        return best_score < threshold or ambiguous

    @staticmethod
    def _classifier_domains(
        routing: dict[str, Any], domains: list[dict[str, Any]], limit: int = 8
    ) -> list[dict[str, Any]]:
        """Rank active domains for the tiny classifier prompt.

        Deterministic candidates come first, then the remaining active domains
        by name. The list is capped so a large domain set cannot make the
        "tiny" tie-break call expensive.
        """
        by_id = {str(item.get("id") or ""): item for item in domains}
        ordered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in routing.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            domain_id = str(candidate.get("domain_id") or "")
            if domain_id in by_id and domain_id not in seen:
                ordered.append(by_id[domain_id])
                seen.add(domain_id)
        for domain in sorted(
            domains, key=lambda item: str(item.get("name") or "")
        ):
            domain_id = str(domain.get("id") or "")
            if domain_id in seen:
                continue
            ordered.append(domain)
            seen.add(domain_id)
        return ordered[:max(1, int(limit))]

    @staticmethod
    def _model_routing_prompt(
        text: str, domains: list[dict[str, Any]]
    ) -> str:
        """Build the compact, tool-free label-classifier prompt."""
        lines = [
            "Choose exactly one active domain label for the user message.",
            "Active domains:",
        ]
        for domain in domains:
            name = " ".join(str(domain.get("name") or "").split())
            description = " ".join(
                str(domain.get("description") or "").split()
            )
            keywords = [
                " ".join(str(item).split())
                for item in domain.get("keywords") or []
                if str(item).strip()
            ]
            lines.append(
                f"{name} ({domain.get('id') or ''}): "
                f"{util.clip(description, 240)} | keywords: "
                f"{util.clip(', '.join(keywords), 360) or 'none'}"
            )
        lines.extend([
            "User message:",
            util.clip(str(text or "").strip(), 4000),
            "Reply with exactly one domain id or general. Output only the label.",
        ])
        return util.clip("\n".join(lines), 4000)

    def _run_model_domain_classifier(
        self, text: str, domains: list[dict[str, Any]]
    ) -> tuple[str | None, str | None]:
        """Run one bounded classifier call and return (choice, raw output)."""
        try:
            prompt = self._model_routing_prompt(text, domains)
        except Exception:
            return None, None
        try:
            timeout = float(
                self.config.get("domains.model_routing_timeout_s", 3.0)
            )
        except (TypeError, ValueError):
            timeout = 3.0
        timeout = max(0.0, timeout)
        try:
            future = self._classifier_pool.submit(
                self.model.chat,
                [{"role": "user", "content": prompt}],
                [],
                max_tokens=MODEL_ROUTING_MAX_TOKENS,
                thinking=False,
                recovery=False,
            )
            try:
                reply = future.result(timeout=timeout)
            except FutureTimeoutError:
                return None, None
            except Exception:
                return None, None
        except Exception:
            return None, None

        try:
            raw = str(getattr(reply, "content", "") or "").strip()
            choice = self._parse_model_domain_choice(raw, domains)
        except Exception:
            return None, None
        return choice, raw

    @staticmethod
    def _parse_model_domain_choice(
        content: str, domains: list[dict[str, Any]]
    ) -> str | None:
        """Parse one leading domain label, without scanning for later text."""
        text = str(content or "").strip()
        if not text:
            return None
        first_line = text.splitlines()[0].strip()
        normalized = " ".join(
            first_line.strip(" \t\"'`").rstrip(".,;:!?").split()
        )
        if not normalized:
            return None
        if normalized.casefold() == "general":
            return "general"
        for domain in domains:
            name = " ".join(str(domain.get("name") or "").split())
            if name and normalized.casefold() == name.casefold():
                return str(domain.get("id") or "")
        match = re.match(r"^[\w:.-]+", first_line, flags=re.UNICODE)
        token = match.group(0) if match else ""
        if not token:
            return None
        for domain in domains:
            if token == str(domain.get("id") or ""):
                return str(domain.get("id") or "")
        for domain in domains:
            name = " ".join(str(domain.get("name") or "").split())
            if name and token.casefold() == name.casefold():
                return str(domain.get("id") or "")
        if token.casefold() == "general":
            return "general"
        return None

    def preview_route(self, text: str) -> dict[str, Any]:
        """Explain a route without recording or changing anything.

        This uses the same learned-affinity merge as normal routing: active
        memory affinity, active domain-skill affinity, task affinity, and open
        task objectives. None of those reads count a memory access, and this
        method deliberately emits no event. It is read-only and does not wait
        for the runtime lock, so route previews remain available during a run.
        """
        text = str(text or "").strip()
        if not text:
            raise ValueError("route preview text must not be empty")
        routing = self.domains.route(
            text,
            open_tasks=self.tasks.list_open(),
            recent_messages=self.messages.recent(limit=10),
            learned=self._routing_learned(text),
        )
        candidates = routing.get("candidates")
        if not isinstance(candidates, list):
            candidates = []
            routing["candidates"] = candidates
        candidate_names: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            name = str(candidate.get("name") or "")
            if not name:
                domain = self.domains.get(str(candidate.get("domain_id") or ""))
                name = str((domain or {}).get("name") or "")
                candidate["name"] = name
            if name and name not in candidate_names:
                candidate_names.append(name)
        routing["candidate_names"] = candidate_names
        return routing

    @staticmethod
    def _suggested_domain_keywords(
        message_text: str, domain: dict[str, Any]
    ) -> list[str]:
        """Suggest up to three message terms missing from domain vocabulary.

        This is advisory only.  Nothing here writes keywords to the domain.
        """
        corpus = " ".join([
            str(domain.get("name") or ""),
            " ".join(str(item) for item in domain.get("keywords") or []),
            " ".join(str(item) for item in domain.get("examples") or []),
            str(domain.get("description") or ""),
        ])
        existing = set(util.stemmed_keywords(corpus))
        suggestions: list[str] = []
        for term in util.keywords(message_text):
            stems = util.stemmed_keywords(term)
            normalized = stems[0] if stems else term
            if normalized in existing:
                continue
            if any(stem in existing for stem in stems):
                continue
            if term not in suggestions:
                suggestions.append(term)
            if len(suggestions) >= 3:
                break
        return suggestions

    @_serialized
    def correct_task_domain(
        self,
        task_id: str,
        domain_id: str,
        *,
        reason: str = "",
        source: str = "web",
    ) -> dict[str, Any]:
        """Move one task to the user's corrected domain and label the fix.

        A correction mutates task state, so it remains serialized and waits
        for any active run to finish before the task is moved.
        """
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(f"no such task: {task_id}")
        domain = self.domains.get(domain_id)
        if domain is None:
            raise KeyError(f"no such domain: {domain_id}")
        if domain.get("status") != "active":
            raise ValueError(f"domain {domain['name']} is archived")

        previous_domain_id = task.get("domain_id")
        if previous_domain_id == domain["id"]:
            return {
                "task": task,
                "feedback": None,
                "suggested_keywords": [],
                "previous_domain_id": previous_domain_id,
            }

        # Capture routing evidence before changing the task's domain.
        task_meta = self.tasks.meta(task_id)
        routing = task_meta.get("routing")
        evidence = dict(routing) if isinstance(routing, dict) else {}
        reason = util.clip(str(reason or "").strip(), 1000)
        source = util.clip(str(source or "web").strip(), 40) or "web"

        message_text = str(task.get("objective") or "").strip()
        if not message_text:
            user_messages = [
                message for message in self.messages.recent_for_task(task_id)
                if message.get("role") == "user"
            ]
            if user_messages:
                message_text = str(user_messages[-1].get("content") or "").strip()

        updated = self.tasks.set_domain(task_id, domain["id"])
        # The task's current domain and its routing evidence must describe the
        # same decision. Keep the correction marker so the UI can explain that
        # the original route was replaced by the user.
        task_meta["routing"] = {
            "domain_id": domain["id"],
            "score": 0.0,
            "reason": "user corrected the domain",
            "hint": None,
            "matched": [],
            "candidates": [],
            "corrected_from": previous_domain_id,
            "corrected_to": domain["id"],
            "model_choice": None,
            "model_route": "off",
        }
        self.tasks.set_meta(task_id, task_meta)
        self.events.record(
            "domain.corrected",
            task_id=task_id,
            domain_id=domain["id"],
            data={
                "from_domain_id": previous_domain_id,
                "to_domain_id": domain["id"],
                "reason": reason,
                "source": source,
                "evidence": evidence,
            },
        )
        feedback_source = source if source in ("ui", "tool", "cli") else "ui"
        feedback = self.app.routing_feedback.record(
            task_id,
            message_text or "routing correction",
            str(previous_domain_id or "general"),
            domain["id"],
            evidence,
            feedback_source,
        )
        suggested_keywords = self._suggested_domain_keywords(
            message_text, domain
        )

        latest_run = self._latest_run(task_id)
        self._announce_domain(
            str(latest_run.get("id") or "") if latest_run else "",
            updated,
            str(previous_domain_id) if previous_domain_id else None,
            "domain corrected by user",
            [],
        )
        return {
            "task": self.tasks.get(task_id) or updated,
            "feedback": feedback,
            "suggested_keywords": suggested_keywords,
            "previous_domain_id": previous_domain_id,
        }

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def _announce_domain(
        self,
        run_id: str,
        task: dict[str, Any],
        previous_domain: str | None,
        reason: str,
        matched: list[str] | None = None,
    ) -> None:
        """Record and optionally display a one-time domain entry status."""
        current_task = self.tasks.get(task["id"]) or task
        current_domain = str(current_task.get("domain_id") or "")
        if not current_domain or current_domain == "general":
            return
        domain = self.domains.get(current_domain)
        if domain is None or domain.get("status") != "active":
            return
        prior = self.db.query_one(
            "SELECT id FROM event_log WHERE type='domain.entered'"
            " AND task_id=? AND domain_id=? LIMIT 1",
            (current_task["id"], current_domain),
        )
        changed = previous_domain != current_domain
        if prior is not None and not changed:
            return
        terms = [
            str(term).strip()
            for term in (matched or [])
            if str(term).strip()
        ]
        self.events.record(
            "domain.entered",
            task_id=current_task["id"],
            run_id=run_id,
            domain_id=current_domain,
            data={
                "domain_id": current_domain,
                "name": domain["name"],
                "reason": reason,
                "matched": terms,
            },
        )
        if not bool(self.config.get("domains.status_line", True)):
            return
        text = f"Entering {domain['name']}"
        if terms:
            text += f" - matched: {', '.join(terms)}"
        self.messages.record(
            "note", text, source="system",
            domain_id=current_domain, task_id=current_task["id"],
            run_id=run_id, visible=self._visible(current_task),
            meta={
                "kind": "domain.status",
                "chip": True,
                "domain_id": current_domain,
            },
        )

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
        task_meta = util.loads(task.get("meta") or "{}", {})
        if not isinstance(task_meta, dict):
            task_meta = {}
        routing = trigger.get("routing")
        if not isinstance(routing, dict):
            routing = task_meta.get("routing")
        if not isinstance(routing, dict):
            routing = None
        role = task.get("role") or "user"
        selection = self._select_tools(task.get("domain_id"), role)
        built = self.context.build(
            task=task, domain=self.domains.get(task["domain_id"])
            if task.get("domain_id") else None,
            trigger=trigger, pending_approvals=pending, run_id=run_id,
            routing=routing, tool_selection=selection, defer_access=True,
        )
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
            "trigger": trigger,
            **self._model_stamp(),
        }
        # Inbox content is untrusted by definition. Seed the same taint list
        # that untrusted tool output uses, so every later side-effecting tool
        # is escalated to an explicit approval by the one existing rule.
        tainted: list[dict[str, Any]] = []
        for source in (
            list(trigger.get("tainted_sources") or [])
            + list((task_meta or {}).get("tainted_sources") or [])
        ):
            if not isinstance(source, dict):
                continue
            entry = {
                "tool": str(source.get("tool") or "inbox"),
                "module": str(source.get("module") or "memory"),
                "origin": util.clip(str(source.get("origin") or "external"), 300),
                "at": source.get("at") or util.iso(),
                "derived": bool(source.get("derived")),
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
        routing_matched = (routing or {}).get("matched")
        self._announce_domain(
            run_id, task, task.get("domain_id"),
            str((routing or {}).get("reason") or "task started in domain"),
            (
                [str(term) for term in routing_matched]
                if isinstance(routing_matched, list) else []
            ),
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
        self._clear_denied_actions(run["id"])
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
        self._clear_denied_actions(run["id"])
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
        self._clear_denied_actions(run["id"])
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
        self,
        approval_id: str,
        decision: str,
        source: str = "web",
        *,
        feedback: str = "",
        arguments: dict[str, Any] | None = None,
        remember: bool = False,
    ) -> RunResult:
        """Decide an approval and run its consequence under one lock.

        This keeps the decision and the claim/execution from interleaving
        with a user message or a scheduler tick.
        """
        approval = self.approvals.get(approval_id)
        if approval is None:
            raise ValueError(f"no such approval: {approval_id}")
        if decision == "approve":
            # Validate the override before deciding, so a failed decision
            # never changes policy; apply it once the decision is committed.
            remember_tool = (
                self._always_allow_tool(approval) if remember else ""
            )
            decided = self.approvals.decide(
                approval_id, "approve", decided_by=source
            )
            if remember_tool:
                self._remember_always_allow(decided, remember_tool)
            return self.resume_after_approval(decided, source)
        if decision == "reject":
            if str(feedback or "").strip():
                revised = self.approvals.revise(
                    approval_id, feedback=feedback, decided_by=source
                )
                return self.resume_after_revision(revised, feedback, source)
            decided = self.approvals.decide(
                approval_id, "reject", decided_by=source
            )
            return self.resume_after_rejection(decided, source)
        if decision == "revise":
            if not str(feedback or "").strip():
                raise ValueError("a revision requires non-blank feedback")
            revised = self.approvals.revise(
                approval_id, feedback=feedback, decided_by=source
            )
            return self.resume_after_revision(revised, feedback, source)
        if decision == "edit":
            if not isinstance(arguments, dict):
                raise ValueError("an edit requires a JSON object of arguments")
            return self.edit_approval(approval_id, arguments, source)
        if decision == "cancel":
            decided = self.approvals.decide(
                approval_id, "cancel", decided_by=source
            )
            return self.resume_after_rejection(decided, source)
        raise ValueError(f"invalid approval decision: {decision}")

    def _always_allow_tool(self, approval: dict[str, Any]) -> str:
        """Validate a narrow always-allow request; return the tool name."""
        action = (approval.get("meta") or {}).get("action", "tool")
        tool = str(approval.get("tool") or "")
        if action != "tool":
            raise ValueError(
                "always allow is available only for tool approvals"
            )
        spec = self.tools.resolve_optional(tool)
        if spec is None:
            raise ValueError(f"always allow requires an existing tool: {tool}")
        if tool in SKILL_WRITE_TOOLS:
            raise ValueError(
                f"always allow is not available for skill write tool {tool}"
            )
        if spec.side_effects:
            raise ValueError(
                f"always allow is not available for side-effecting tool {tool}"
            )
        if self._taint_sources(approval["run_id"]):
            raise ValueError(
                "always allow is not available in a run with untrusted sources"
            )
        return tool

    def _remember_always_allow(
        self, approval: dict[str, Any], tool: str
    ) -> None:
        """Persist the validated always-allow override for one tool."""
        try:
            self.tools.set_risk_override(tool, "low")
        except ToolError as exc:
            raise ValueError(str(exc)) from exc
        self.events.record(
            "approval.remembered", task_id=approval["task_id"],
            run_id=approval["run_id"],
            data={
                "approval_id": approval["id"],
                "tool": tool,
                "risk_override": "low",
            },
        )

    @_serialized
    def edit_approval(
        self,
        approval_id: str,
        arguments: dict[str, Any],
        source: str,
    ) -> RunResult:
        """Validate edited arguments, supersede the request, and execute it."""
        approval = self.approvals.get(approval_id)
        if approval is None:
            raise ValueError(f"no such approval: {approval_id}")
        if approval.get("status") != "pending":
            raise ApprovalError(
                f"approval {approval_id} is already {approval.get('status')}"
            )
        expires_at = approval.get("expires_at")
        if expires_at and util.parse_iso(expires_at) < util.now_utc():
            raise ApprovalError(f"approval {approval_id} expired")
        action = (approval.get("meta") or {}).get("action", "tool")
        if action not in ("tool", "create_domain"):
            raise ValueError(f"approval action cannot be edited: {action}")
        # Validate the edit against the definition this run froze when the
        # approval was requested, not the live registry: a reload must not
        # reject arguments that were valid when the user was asked to approve
        # them, nor accept arguments the frozen schema never allowed.
        spec = self.approval_tool_spec(approval)
        if spec is None:
            raise ValueError(
                f"approval tool does not exist: {approval.get('tool')}"
            )
        validated = validate_arguments(spec, arguments)

        taint = self._taint_sources(approval["run_id"])
        if taint and spec.side_effects and not spec.read_only:
            if spec.risk != "confirm":
                self.events.record(
                    "trust.escalated", task_id=approval["task_id"],
                    run_id=approval["run_id"],
                    data={
                        "tool": spec.name,
                        "module": spec.module,
                        "from_risk": spec.risk,
                        "origin": str(taint[0].get("origin") or ""),
                        "via": "user_edit",
                    },
                )

        old_reason = str(approval.get("reason") or "").strip()
        reason = (
            f"{old_reason} (edited by user)" if old_reason
            else "(edited by user)"
        )
        replacement = self.approvals.supersede(
            approval_id,
            arguments=validated,
            reason=reason,
            risk=str(approval.get("risk") or spec.risk),
            decided_by=f"edit:{source}",
        )
        decided = self.approvals.decide(
            replacement["id"], "approve", decided_by=f"edit:{source}"
        )
        return self.resume_after_approval(decided, source)

    def summary_for(self, approval: dict[str, Any]) -> str:
        """Human summary for an approval, using the run's frozen template."""
        spec = self.approval_tool_spec(approval)
        if spec is not None and spec.summary_template:
            return self.approvals.summarize(
                approval, spec.summary_template
            )
        return self.approvals.describe(approval)

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
        # The run's taint still applies to the approved execution: a
        # tainted memory write must stay provenance-marked at the moment
        # the memory is actually written.
        ctx.tainted_origin = self._first_taint_origin(run["id"])
        snapshot = self._toolset(run["id"])
        spec = self._frozen_spec(run["id"], call.name)
        outcome = self.tools.execute(
            call, ctx, run_id=run["id"], task_id=task["id"],
            execution_id=execution_id, approval_id=approval["id"],
            role=task.get("role") or "user", snapshot=snapshot,
        )
        if outcome.get("status") == "succeeded" and spec is not None:
            self._record_result_taint(
                run["id"], call.name, spec, outcome.get("result")
            )
        self.approvals.mark_executed(approval["id"])
        if (approval.get("meta") or {}).get("edited_from"):
            edited = dict(outcome)
            if "result" in edited:
                result = edited["result"]
                if isinstance(result, dict):
                    edited["result"] = {
                        **result,
                        "user_edited_arguments": approval["arguments"],
                    }
                else:
                    edited["result"] = {
                        "result": result,
                        "user_edited_arguments": approval["arguments"],
                    }
            else:
                edited["user_edited_arguments"] = approval["arguments"]
            outcome = edited
        self._append_tool_result(
            run["id"], call.id, outcome, approval["id"],
            untrusted=bool(
                spec is not None and (
                    spec.untrusted_output
                    or self._dynamic_taint_sources(outcome.get("result"))
                )
            ),
            tool=call.name,
            origin=self._first_taint_origin(run["id"]),
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
        current = self.approvals.get(approval["id"]) or approval
        if current.get("status") == "rejected":
            self._remember_denied_action(
                run["id"], current["tool"], current.get("arguments") or {}
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

    @_serialized
    def resume_after_revision(
        self, approval: dict[str, Any], feedback: str, source: str
    ) -> RunResult:
        """Close a revised request and let the model propose a replacement."""
        run = self._run_row(approval["run_id"])
        task = self.tasks.get(approval["task_id"])
        if run is None or task is None or run["status"] != "waiting_approval":
            return RunResult(
                "stale", approval["run_id"], approval["task_id"],
                message="That approval no longer matches the pending action.",
            )
        current = self.approvals.get(approval["id"]) or approval
        self._clear_denied_actions(run["id"])
        call_id = current.get("tool_call_id") or "revised_call"
        self._append_tool_result(
            run["id"], call_id,
            {
                "status": "revision_requested",
                "feedback": util.clip(str(feedback or ""), 2000),
                "note": (
                    "the user asked for a change; propose the revised action "
                    "if it is still needed"
                ),
            },
            current["id"],
        )
        execution_id = self._awaiting_execution_id(current["id"])
        if execution_id:
            self.db.execute(
                "UPDATE tool_executions SET status='denied', ended_at=?,"
                " error='revised by user' WHERE id=?",
                (util.iso(), execution_id),
            )
        self._save_run(run["id"], status="running", ended_at=None)
        self.tasks.set_status(task["id"], "active", reason="revised")
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
        run_meta = util.loads(run.get("meta") or "", {})
        if not isinstance(run_meta, dict):
            run_meta = {}
        snapshot_model = str(run_meta.get("model") or "")
        snapshot_provider = str(run_meta.get("provider") or "")
        current = self._model_stamp()
        if not snapshot_model:
            self._save_meta(run_id, {**run_meta, **current})
            snapshot_model = current["model"]
            snapshot_provider = current["provider"]
        elif (snapshot_model, snapshot_provider) != (
            current["model"], current["provider"]
        ):
            self._save_meta(run_id, {**run_meta, **current})
            self.events.record(
                "model.changed", task_id=run["task_id"], run_id=run_id,
                data={
                    "from": snapshot_model,
                    "to": current["model"],
                    "from_provider": snapshot_provider,
                    "to_provider": current["provider"],
                    "source": "resume",
                },
            )
            snapshot_model = current["model"]
            snapshot_provider = current["provider"]
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
                transcript, tools_schema, run_id=run_id
            )
            if repairs:
                self._save_run(run_id, transcript=util.dumps(repaired))
                self.events.record(
                    "transcript.repaired", level="warn", task_id=task["id"],
                    run_id=run_id, data={"repairs": repairs[:8]},
                )
            try:
                reply = self.model.chat(
                    prompt, tools_schema,
                    model=snapshot_model, provider=snapshot_provider,
                )
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
                self._reconcile_prompt_context(run_id, prompt, extra)
                self.events.record(
                    "model.context_retry", level="warn", task_id=task["id"],
                    run_id=run_id,
                    data={"budget_tokens": tighter, "repairs": extra[:8]},
                )
                try:
                    reply = self.model.chat(
                        prompt, tools_schema,
                        model=snapshot_model, provider=snapshot_provider,
                    )
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
                    "model": snapshot_model,
                    "provider": snapshot_provider,
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
                    spec = self._frozen_spec(run_id, call.name)
                    self._append_tool_result(
                        run_id, call.id, outcome,
                        outcome.get("approval_id"),
                        untrusted=bool(
                            spec is not None and (
                                spec.untrusted_output
                                or self._dynamic_taint_sources(outcome.get("result"))
                            )
                        ),
                        tool=call.name,
                        origin=self._first_taint_origin(run_id),
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
                message_meta = None
                if any(
                    not source.get("derived")
                    for source in self._taint_sources(run_id)
                ):
                    message_meta = {
                        "derived_untrusted": True,
                        "tainted_origin": self._first_taint_origin(run_id),
                    }
                note_id = self.messages.record(
                    "assistant", content, source="agent", task_id=task["id"],
                    run_id=run_id, domain_id=task.get("domain_id"),
                    visible=self._visible(task), meta=message_meta,
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

    def _skill_denial_arguments(
        self, run_id: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Return the arguments a skill call would put in its approval."""
        skills = self.app.modules.skills
        pending = self.approvals.pending_for_run(run_id)
        name = str(arguments.get("name") or "").strip()
        pending_name = str(
            ((pending or {}).get("arguments") or {}).get("name") or ""
        ).strip().lower()
        refining = (
            pending is not None
            and tool in SKILL_WRITE_TOOLS
            and pending.get("tool") in SKILL_WRITE_TOOLS
            and bool(name)
            and name.lower() == pending_name
        )
        try:
            if refining:
                return skills.merge_fields(
                    dict(pending.get("arguments") or {}),
                    description=arguments.get("description"),
                    preconditions=arguments.get("preconditions"),
                    domains=arguments.get("domains"),
                    body=arguments.get("body"),
                )
            if tool == "edit_skill":
                _, planned = skills.edit_plan(
                    name=name,
                    description=arguments.get("description"),
                    preconditions=arguments.get("preconditions"),
                    domains=arguments.get("domains"),
                    body=arguments.get("body"),
                )
                return planned
            return skills.validate_fields(
                name=name,
                description=str(arguments.get("description") or ""),
                preconditions=arguments.get("preconditions"),
                domains=arguments.get("domains"),
                body=str(arguments.get("body") or ""),
            )
        except ValueError:
            # The normal tool path will report the precise validation error.
            return arguments

    def _rejected_approval_id(
        self, run_id: str, tool: str, args_hash: str
    ) -> str:
        """Find the rejected approval for an exact action, if still known."""
        rows = self.db.query(
            "SELECT id, arguments FROM approvals WHERE run_id=? AND tool=?"
            " AND status='rejected' ORDER BY created_at DESC, rowid DESC",
            (run_id, tool),
        )
        for row in rows:
            arguments = util.loads(row["arguments"] or "{}", {})
            if self._args_hash(arguments) == args_hash:
                return str(row["id"])
        return ""

    def _repeat_denied_result(
        self,
        run_id: str,
        task_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Deny an exact action the user already rejected in this run."""
        args_hash = self._args_hash(arguments)
        denied = self._denied_actions(run_id)
        if not any(
            entry.get("tool") == tool
            and entry.get("args_hash") == args_hash
            for entry in denied
        ):
            return None
        approval_id = self._rejected_approval_id(run_id, tool, args_hash)
        self.events.record(
            "approval.repeat_denied", level="warn", task_id=task_id,
            run_id=run_id,
            data={"tool": tool, "approval": approval_id},
        )
        return {
            "status": "denied",
            "error": (
                "The user already rejected this exact action in this run. "
                "Do not propose it again unless the user asks for it anew."
            ),
        }

    def _domain_briefing(
        self,
        run_id: str,
        task: dict[str, Any],
        previous_domain: str | None,
        new_domain_id: str,
    ) -> dict[str, Any]:
        """Build the environment briefing delivered by ``use_domain``."""
        domain = self.domains.get(new_domain_id)
        if domain is None or domain.get("status") != "active":
            return {"text": "", "memories": [], "skills": [], "new_tools": []}
        objective = str(task.get("objective") or "")

        memories = self.memory.search(
            objective,
            scope_priority=[f"domain:{new_domain_id}", "global"],
            limit=3,
        )
        memory_items: list[dict[str, Any]] = []
        untrusted_sources: list[dict[str, Any]] = []
        for item in memories:
            provenance = item.get("provenance") or {}
            if isinstance(provenance, dict) and provenance.get("tainted"):
                untrusted_sources.append({
                    "tool": f"memory:{item['id']}", "module": "memory",
                    "origin": util.clip(
                        str(provenance.get("tainted_origin") or "external"), 300
                    ),
                    "at": util.iso(), "derived": False,
                })
            memory_items.append({
                "id": item["id"],
                "kind": item.get("kind") or "memory",
                "scope": item.get("scope") or "",
                "content": util.clip(str(item.get("content") or ""), 200),
            })
        if memory_items:
            try:
                self.memory.record_access(
                    [item["id"] for item in memory_items], run_id=run_id
                )
            except Exception:
                pass

        skill_items: list[dict[str, Any]] = []
        skills = self.app.modules.skills
        # Use the same disclosure rules as the context builder so adaptive
        # skills whose preconditions match the task also appear in a mid-run
        # briefing (an entry briefing is the only skill disclosure that run).
        _, skill_sources = skills.disclose(new_domain_id, objective)
        applicable_skill_ids = {
            str(source.get("ref") or "")
            for source in skill_sources
            if str(source.get("reason") or "") in (
                "global skill",
                "domain-bound skill for this domain",
                "skill preconditions matched the objective",
            )
        }
        for module in skills.all():
            if len(skill_items) >= 5:
                break
            if module.status != "active" or module.id not in applicable_skill_ids:
                continue
            description = " ".join(
                str((module.manifest or {}).get("description") or "").split()
            )
            skill_items.append({
                "name": module.name,
                "description": util.clip(description, 160),
            })

        role = task.get("role") or "user"
        previous_selection = self._select_tools(previous_domain, role)
        new_selection = self._select_tools(new_domain_id, role)
        previous_names = set(previous_selection.get("names") or [])
        new_tools = [
            name for name in (new_selection.get("names") or [])
            if name not in previous_names
        ]

        try:
            instruction_limit = int(
                self.config.get("domains.instructions_max_chars", 1200)
            )
        except (TypeError, ValueError):
            instruction_limit = 1200
        instruction_limit = max(1, instruction_limit)
        description = util.clip(str(domain.get("description") or ""), 400)
        instructions = util.clip(
            str(domain.get("instructions") or "").strip(), instruction_limit
        )
        lines = [f"# Entering {domain['name']}"]
        if description:
            lines.append(f"Description: {description}")
        if instructions:
            lines.append("Instructions:")
            lines.append(instructions)
        if memory_items:
            lines.append("Relevant memories:")
            for item in memory_items:
                lines.append(
                    f"- [{item['kind']}] ({item['scope']}): {item['content']}"
                )
        if skill_items:
            lines.append("Applicable skills:")
            for item in skill_items:
                suffix = f": {item['description']}" if item["description"] else ""
                lines.append(f"- {item['name']}{suffix}")
        if new_tools:
            lines.append("Newly available tools:")
            lines.extend(f"- {name}" for name in new_tools)
        try:
            briefing_limit = int(
                self.config.get("domains.briefing_chars", 1800)
            )
        except (TypeError, ValueError):
            briefing_limit = 1800
        briefing_limit = max(1, briefing_limit)
        text = util.clip("\n".join(lines), briefing_limit)

        self._append_context_source(
            run_id,
            {
                "section": "domain",
                "ref": f"domain:{new_domain_id}:briefing",
                "reason": "domain entry briefing",
            },
        )
        for item in memories:
            self._append_context_source(
                run_id,
                {
                    "section": "memories",
                    "ref": f"memory:{item['id']}",
                    "reason": str(item.get("reason") or "domain briefing memory"),
                },
            )
        for item in skill_items:
            self._append_context_source(
                run_id,
                {
                    "section": "domain",
                    "ref": f"skill:{item['name']}",
                    "reason": "active skill disclosed in domain briefing",
                },
            )
        for name in new_tools:
            self._append_context_source(
                run_id,
                {
                    "section": "tools",
                    "ref": name,
                    "reason": "newly available after domain entry",
                },
            )
        return {
            "text": text,
            "memories": memory_items,
            "skills": skill_items,
            "new_tools": new_tools,
            "untrusted_sources": untrusted_sources,
        }

    def _rebuild_context_after_domain_change(
        self,
        run: dict[str, Any],
        task: dict[str, Any],
        previous_domain: str | None,
    ) -> None:
        """Replace the run's system context after a domain switch.

        The task, memories, skills, and tool selection may all change when the
        model enters a domain. Rebuilding the prompt keeps those changes
        atomic from the model's point of view and removes instructions and
        routing hints belonging to the previous domain.
        """
        run_id = str(run["id"])
        fresh = self.tasks.get(str(task["id"]))
        if fresh is None:
            return
        new_domain_id = str(fresh.get("domain_id") or "general")
        new_domain = self.domains.get(new_domain_id)
        role = fresh.get("role") or "user"
        trigger = self._run_meta(run_id).get("trigger")
        if not isinstance(trigger, dict):
            trigger = {"kind": "domain_change"}
        built = self.context.build(
            task=fresh,
            domain=new_domain,
            trigger=trigger,
            pending_approvals=self.approvals.pending(),
            run_id=run_id,
            # The original route described the pre-switch task. Keeping it
            # would put a stale domain hint back into the rebuilt prompt.
            routing=None,
            tool_selection=self._select_tools(new_domain_id, role),
            defer_access=True,
        )

        current = self._run_row(run_id) or run
        transcript = self._transcript(current)
        if (
            transcript
            and isinstance(transcript[0], dict)
            and transcript[0].get("role") == "system"
        ):
            transcript[0]["content"] = built["system"]
        else:
            transcript.insert(0, {"role": "system", "content": built["system"]})
        sources = util.loads(current.get("context_sources") or "[]", [])
        if not isinstance(sources, list):
            sources = []
        sources.extend(
            source for source in built.get("sources", [])
            if isinstance(source, dict)
        )
        sources.append({
            "section": "domain",
            "ref": "domain:rebuild",
            "reason": "system prompt rebuilt after domain change",
        })
        self._save_run(
            run_id,
            transcript=util.dumps(transcript),
            context_sources=util.dumps(sources),
        )
        self.events.record(
            "domain.context_rebuilt",
            task_id=fresh["id"],
            run_id=run_id,
            domain_id=new_domain_id,
            data={
                "from_domain_id": previous_domain,
                "to_domain_id": new_domain_id,
            },
        )

    def _handle_tool_call(
        self, run: dict[str, Any], task: dict[str, Any], call: ToolCall
    ) -> dict[str, Any]:
        run_id, task_id = run["id"], task["id"]
        previous_domain = task.get("domain_id")
        role = task.get("role") or "user"
        # Everything below uses the run's frozen snapshot: the schema shown to
        # the model, argument validation, and the risk/trust policy. A module
        # reload mid-run cannot change any of them. A tool removed since the
        # run started is not resolved at all (fail closed, never resurrected).
        snapshot = self._toolset(run_id)
        try:
            known = self.tools.resolve_frozen(call.name, snapshot=snapshot)
        except (ToolError, ToolDenied):
            # A live tool outside this run's snapshot still needs its
            # role restriction reported as ``blocked`` (sub-agents cannot use
            # confirm-risk or delegation tools); a removed tool resolves to
            # ``None`` and fails closed below instead.
            known = self.tools.resolve_optional(call.name)
        validated_arguments: dict[str, Any] | None = None
        if known is not None:
            try:
                validated_arguments = validate_arguments(
                    known, call.arguments
                )
            except ToolError:
                # Preserve the existing, tool-specific validation error path.
                validated_arguments = None
        if validated_arguments is not None:
            if call.name in SKILL_WRITE_TOOLS:
                validated_arguments = self._skill_denial_arguments(
                    run_id, call.name, validated_arguments
                )
            denied = self._repeat_denied_result(
                run_id, task_id, call.name, validated_arguments
            )
            if denied is not None:
                return denied
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
        try:
            spec = self.tools.resolve_frozen(
                call.name, snapshot=snapshot, role=role
            )
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
        if call.name in DOMAIN_WRITE_TOOLS and taint:
            origin = str(taint[0].get("origin") or "untrusted content")
            wanted = str((call.arguments or {}).get("domain") or "").strip()
            domain = self.domains.get(wanted) or self.domains.by_name(wanted)
            self.events.record(
                "domain.refused", level="warn", task_id=task_id, run_id=run_id,
                source="runtime",
                data={
                    "origin": util.clip(origin, 300),
                    "tool": call.name,
                    "domain": str((domain or {}).get("id") or wanted)[:120],
                    "name": str((domain or {}).get("name") or wanted)[:120],
                },
            )
            return {
                "status": "denied",
                "error": (
                    "This run consumed untrusted content from "
                    f"{util.clip(origin, 160)}, so it cannot change "
                    "user-authored domain instructions."
                ),
            }
        tainted_side_effect = bool(
            taint and spec.side_effects and not spec.read_only
        )
        tainted_egress = bool(taint and spec.external_egress)
        tainted_action = tainted_side_effect or tainted_egress
        escalated = tainted_action and spec.risk != "confirm"
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
                    "external_egress": tainted_egress,
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
                    call, role=role, snapshot=snapshot
                )
            except Exception as exc:
                return {"status": "denied", "error": str(exc)}
            if call.name in DOMAIN_WRITE_TOOLS:
                wanted = str(arguments.get("domain") or "").strip()
                domain = self.domains.get(wanted) or self.domains.by_name(wanted)
                if domain is None:
                    return {
                        "status": "denied",
                        "error": (
                            f"no such domain: {wanted}; use an existing "
                            "domain id or name"
                        ),
                    }
                if domain.get("status") != "active":
                    return {
                        "status": "denied",
                        "error": f"domain {domain['name']} is archived",
                    }
                try:
                    instruction_limit = int(
                        self.config.get("domains.instructions_max_chars", 1200)
                    )
                except (TypeError, ValueError):
                    instruction_limit = 1200
                instruction_limit = max(1, instruction_limit)
                instructions = str(arguments.get("instructions") or "")
                if "\x00" in instructions:
                    return {
                        "status": "denied",
                        "error": "domain instructions must not contain NUL bytes",
                    }
                arguments["domain"] = wanted
                arguments["instructions"] = util.clip(
                    instructions, instruction_limit
                )
                arguments["reason"] = util.clip(
                    str(arguments.get("reason") or ""), 500
                )
                # The approval and its eventual execution must carry exactly
                # the same bounded instructions shown to the user.
                call.arguments = arguments
                call.raw_arguments = util.dumps(arguments)
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
                f"before {call.name} can change state or transmit data"
                if tainted_action else f"confirm-risk tool: {call.name}"
            )
            revised = self.db.query_one(
                "SELECT id FROM approvals WHERE run_id=? AND tool=?"
                " AND status='revised' ORDER BY created_at DESC, rowid DESC"
                " LIMIT 1",
                (run_id, call.name),
            )
            supersedes_id = str(revised["id"]) if revised else None
            approval = self.approvals.create(
                task_id=task_id, run_id=run_id, action=action,
                tool=call.name, arguments=arguments,
                risk=effective_risk, tool_call_id=call.id,
                reason=reason, supersedes_id=supersedes_id,
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
                "so a state-changing or external-network action needs your explicit approval."
                if tainted_action else ""
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
            elif call.name in DOMAIN_WRITE_TOOLS:
                note = (
                    "I want to change **user-authored domain instructions**. "
                    "Review the full bounded text below; approval is required "
                    "before anything is written.\n\n"
                    f"{self._domain_proposal_detail(arguments)}\n\n"
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
        # Carry the taint origin into execution so memory handlers can
        # mark provenance when a (non-escalated) write happens in a
        # tainted run.
        ctx.tainted_origin = taint_origin
        ctx.briefing = lambda new_domain_id: self._domain_briefing(
            run_id, task, previous_domain, new_domain_id
        )
        outcome = self.tools.execute(
            call, ctx, run_id=run_id, task_id=task_id,
            role=role, snapshot=snapshot,
        )
        if outcome.get("status") == "succeeded":
            self._record_result_taint(
                run_id, call.name, spec, outcome.get("result")
            )
        if outcome.get("status") == "succeeded" and call.name == "use_domain":
            self._announce_domain(
                run_id, task, previous_domain,
                "domain changed by the model", [],
            )
            self._rebuild_context_after_domain_change(
                run, task, previous_domain,
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

    def _domain_proposal_detail(self, arguments: dict[str, Any]) -> str:
        """Bounded, readable approval content for a domain instruction edit."""
        wanted = str(arguments.get("domain") or "").strip()
        domain = self.domains.get(wanted) or self.domains.by_name(wanted)
        name = str((domain or {}).get("name") or wanted or "(missing)")
        domain_id = str((domain or {}).get("id") or wanted or "(missing)")
        try:
            limit = int(
                self.config.get("domains.instructions_max_chars", 1200)
            )
        except (TypeError, ValueError):
            limit = 1200
        limit = max(1, limit)
        instructions = util.clip(
            str(arguments.get("instructions") or ""), limit
        )
        reason = util.clip(str(arguments.get("reason") or "").strip(), 500)
        return (
            f"- domain: {name} ({domain_id})\n"
            f"- reason: {reason or '(none)'}\n\n"
            f"Instructions (limit {limit} chars):\n{instructions}"
        )

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
        approval_id: str | None = None, *,
        untrusted: bool = False,
        tool: str = "",
        origin: str = "",
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
        status = str(outcome.get("status") or "")
        if untrusted and status not in (
            "awaiting_approval", "deferred", "unknown"
        ):
            text = util.untrusted_envelope(text, tool=tool, origin=origin)
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

"""Sequential, bounded sub-agents.

A delegation is a normal child task and run: it uses the one runtime loop, the
same database, exact approvals, and the same recovery rules. The child gets a
separate, reduced context and a low-risk-only toolset; the parent receives one
tool result and never shares its transcript.

Depth is one. Parallel children, worker pools, and a second runtime are
explicitly out of scope. The parent's ``RLock`` is re-entrant, so the tool
handler can ask the runtime to execute the child run inline.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from . import util


class SubAgentManager:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.config = app.config
        self.events = app.events
        self.tasks = app.tasks
        self.runtime = app.runtime
        self.artifacts_dir: Path = app.artifacts_dir

    # ------------------------------------------------------------------
    # Public entry point: the ``delegate_task`` tool handler
    # ------------------------------------------------------------------

    def delegate(self, ctx, args: dict[str, Any]) -> dict[str, Any]:
        if not bool(self.config.get("subagents.enabled", True)):
            return self._blocked(None, "sub-agents are disabled in configuration")

        parent_task = self.tasks.get(ctx.task_id)
        parent_run = self.runtime._run_row(ctx.run_id)
        if parent_task is None or parent_run is None:
            return self._blocked(None, "the parent task or run no longer exists")
        if (parent_task.get("role") or "user") == "subagent":
            return self._blocked(None, "sub-agents cannot delegate again")

        parent_meta = util.loads(parent_task.get("meta") or "{}", {})
        parent_depth = int(parent_meta.get("depth", 0) or 0)
        max_depth = max(0, int(self.config.get("subagents.max_depth", 1)))
        if parent_depth >= max_depth:
            return self._blocked(
                None,
                f"maximum sub-agent depth reached ({max_depth})",
            )

        goal = str(args.get("goal") or "").strip()
        if not goal:
            return self._blocked(None, "a delegation needs a non-empty goal")
        context_text = str(args.get("context") or "").strip()
        expected = str(args.get("expected_output") or "").strip()

        goal_hash = self._goal_hash(goal)
        origin_ref = f"{parent_task['id']}:{goal_hash}"

        # Re-delegating the same goal reuses the existing child rather than
        # spawning a duplicate. Completed children contribute their stored
        # result; anything else is reported as blocked, never silently retried.
        existing = self._find_child(origin_ref, parent_task["id"])
        if existing is not None:
            return self._reuse(existing, parent_task)

        max_children = max(0, int(
            self.config.get("subagents.max_children_per_run", 3)
        ))
        spawned = self.db.query_one(
            "SELECT COUNT(*) AS c FROM tasks WHERE parent_run_id=?"
            " AND role='subagent'",
            (parent_run["id"],),
        )
        if spawned is not None and int(spawned["c"]) >= max_children:
            return self._blocked(
                None,
                f"this run already used its {max_children} child budget",
            )

        budget = {
            "max_iterations": int(
                self.config.get("subagents.max_iterations", 8)
            ),
            "max_tool_calls": int(
                self.config.get("subagents.max_tool_calls", 12)
            ),
            "max_seconds": float(
                self.config.get("subagents.max_seconds", 180.0)
            ),
        }
        meta = {
            "notes": "",
            "depth": parent_depth + 1,
            "goal_hash": goal_hash,
            "parent_context": util.clip(context_text, 3000),
            "expected_output": util.clip(expected, 600),
            "budget": budget,
            "parent_run_id": parent_run["id"],
        }
        child = self.tasks.create(
            objective=goal,
            domain_id=parent_task.get("domain_id"),
            origin="subagent",
            origin_ref=origin_ref,
            meta=meta,
            parent_task_id=parent_task["id"],
            parent_run_id=parent_run["id"],
            role="subagent",
        )
        self.events.record(
            "subagent.started",
            task_id=child["id"],
            run_id=parent_run["id"],
            domain_id=parent_task.get("domain_id"),
            data={
                "parent_task_id": parent_task["id"],
                "parent_run_id": parent_run["id"],
                "child_task_id": child["id"],
                "goal_hash": goal_hash,
                "goal": util.clip(goal, 300),
            },
        )

        result = self.runtime.start_run(
            child,
            {
                "kind": "subagent",
                "name": util.clip(goal, 100),
                "parent_run_id": parent_run["id"],
            },
            trigger_text=goal,
        )
        child = self.tasks.get(child["id"]) or child

        if result.status == "completed":
            return self._complete(child, result, parent_task, goal_hash)

        reason = self._blocked_reason(result)
        self._close_child(child, reason)
        self.events.record(
            "subagent.blocked",
            level="warn",
            task_id=child["id"],
            run_id=parent_run["id"],
            domain_id=parent_task.get("domain_id"),
            data={
                "parent_task_id": parent_task["id"],
                "parent_run_id": parent_run["id"],
                "child_task_id": child["id"],
                "goal_hash": goal_hash,
                "reason": util.clip(reason, 300),
                "status": result.status,
            },
        )
        return {
            "status": "blocked",
            "child_task_id": child["id"],
            "reason": util.clip(reason, 1000),
            "result": util.clip(result.message or reason, self._max_result_chars()),
        }

    # ------------------------------------------------------------------
    # Child completion / reuse
    # ------------------------------------------------------------------

    def _complete(
        self,
        child: dict[str, Any],
        result,
        parent_task: dict[str, Any],
        goal_hash: str,
    ) -> dict[str, Any]:
        content = result.message or ""
        limit = self._max_result_chars()
        clipped = util.clip(content, limit)
        rel_path = f"subagents/{child['id']}.md"
        self._write_artifact(child, rel_path, result, clipped)

        meta = self.tasks.meta(child["id"])
        meta["result"] = clipped
        meta["result_path"] = rel_path
        meta["result_run_id"] = result.run_id
        meta["goal_hash"] = goal_hash
        self.tasks.set_meta(child["id"], meta)

        self.events.record(
            "subagent.completed",
            task_id=child["id"],
            run_id=result.run_id,
            domain_id=parent_task.get("domain_id"),
            data={
                "parent_task_id": parent_task["id"],
                "child_task_id": child["id"],
                "child_run_id": result.run_id,
                "goal_hash": goal_hash,
                "artifact_path": rel_path,
            },
        )
        return {
            "status": "completed",
            "child_task_id": child["id"],
            "child_run_id": result.run_id,
            "result": clipped,
            "artifact_path": rel_path,
            "reused": False,
        }

    def _reuse(
        self, child: dict[str, Any], parent_task: dict[str, Any]
    ) -> dict[str, Any]:
        meta = self.tasks.meta(child["id"])
        status = child.get("status") or ""
        self.events.record(
            "subagent.reused",
            task_id=child["id"],
            domain_id=parent_task.get("domain_id"),
            data={
                "parent_task_id": parent_task["id"],
                "child_task_id": child["id"],
                "status": status,
            },
        )
        if status == "completed" and meta.get("result"):
            return {
                "status": "completed",
                "child_task_id": child["id"],
                "child_run_id": meta.get("result_run_id"),
                "result": util.clip(
                    str(meta.get("result") or ""), self._max_result_chars()
                ),
                "artifact_path": meta.get("result_path"),
                "reused": True,
            }
        reason = meta.get("blocked_reason") or (
            f"the existing child delegation is {status or 'unfinished'}"
        )
        return {
            "status": "blocked",
            "child_task_id": child["id"],
            "reason": util.clip(str(reason), 1000),
            "reused": True,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _goal_hash(goal: str) -> str:
        normalized = " ".join(goal.lower().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def _find_child(
        self, origin_ref: str, parent_task_id: str
    ) -> dict[str, Any] | None:
        for task in self.tasks.find_by_origin_ref(origin_ref):
            if (
                task.get("parent_task_id") == parent_task_id
                and (task.get("role") or "user") == "subagent"
            ):
                return task
        return None

    def _max_result_chars(self) -> int:
        return max(1, int(
            self.config.get("subagents.max_result_chars", 4000)
        ))

    def _blocked_reason(self, result) -> str:
        status = result.status or "failed"
        if status == "waiting_user":
            return "the sub-agent asked the user a question"
        if status == "waiting_approval":
            return "the sub-agent needed an approval-gated action"
        if status == "limit_reached":
            return f"the sub-agent reached a budget limit: {result.detail}"
        if status == "blocked":
            return result.detail or result.message or "the sub-agent was blocked"
        return (
            f"the sub-agent did not complete ({status}): "
            f"{result.detail or result.message or 'no result'}"
        )

    def _close_child(self, child: dict[str, Any], reason: str) -> None:
        """Make a non-completed child terminal so it cannot hang the parent.

        A limit, a question, or a model failure is a blocked outcome for the
        parent. Child tasks are not user-resumable, so they are cancelled
        explicitly instead of staying paused near an open parent.
        """
        current = self.tasks.get(child["id"])
        if current is not None and current.get("status") in (
            "completed", "failed", "cancelled",
        ):
            return
        meta = self.tasks.meta(child["id"])
        meta["blocked_reason"] = util.clip(reason, 600)
        self.tasks.set_meta(child["id"], meta)
        try:
            self.runtime.cancel_task(child["id"])
        except Exception:
            # The child may already be terminal; never let cleanup hide the
            # parent's blocked result.
            self.tasks.set_status(
                child["id"], "cancelled",
                reason=f"blocked: {util.clip(reason, 200)}",
            )

    def _blocked(self, child_id: str | None, reason: str) -> dict[str, Any]:
        return {
            "status": "blocked",
            "child_task_id": child_id,
            "reason": reason,
        }

    def _write_artifact(
        self, child: dict[str, Any], rel_path: str, result, content: str
    ) -> None:
        path = self.artifacts_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = self.tasks.meta(child["id"])
        context = str(meta.get("parent_context") or "").strip()
        expected = str(meta.get("expected_output") or "").strip()
        lines = [
            "# Sub-agent result",
            "",
            f"- Goal: {child['objective']}",
            f"- Child task: {child['id']}",
            f"- Child run: {result.run_id}",
            f"- Status: {result.status}",
            f"- Completed: {util.iso()}",
        ]
        if child.get("parent_task_id"):
            lines.append(f"- Parent task: {child['parent_task_id']}")
        if expected:
            lines.append(f"- Expected output: {expected}")
        lines.extend(["", "## Result", "", content or "(no final reply)"])
        if context:
            lines.extend(["", "## Parent-supplied context", "", context])
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

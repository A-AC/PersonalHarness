"""Approvals: bounded, one-time authorization for exact actions.

An approval is tied to one pending action: the tool (or virtual action such
as ``create_domain``), the exact arguments, the run, and the risk class. A
generic "yes" resolves at most one pending approval and never grants
standing authority; ambiguous replies never approve; a stale or expired
approval cannot execute. The quick-confirmation path is a classifier only —
it cannot execute tools and cannot decide what is authorized; the runtime
verifies the approval is still valid and unchanged before executing.

Approval state lives in SQLite, so it survives restarts. Statuses:
pending -> approved -> executed, or pending -> rejected | cancelled |
expired. Approving is not executing: the runtime performs the action and
records the real outcome against the approval.
"""

from __future__ import annotations

from typing import Any

from . import util
from .db import Database, rows_to_dicts
from .model import Model, ModelUnavailable, ModelResponseError
from .store import Events

APPROVAL_ACTIONS = ("tool", "create_domain")

_AFFIRMATIVE = (
    "yes", "y", "yeah", "yep", "approve", "approved", "ok", "okay", "confirm",
    "confirmed", "do it", "go ahead", "proceed", "sure", "please do",
    "that's fine", "fine", "accepted", "allow it", "permit", "grant",
)
_NEGATIVE = (
    "no", "n", "nope", "reject", "rejected", "deny", "denied", "cancel",
    "cancelled", "canceled", "stop", "don't", "dont", "do not", "absolutely not",
    "never", "refuse", "block it",
)


class ApprovalError(ValueError):
    pass


class Approvals:
    def __init__(self, db: Database, events: Events, config, model: Model):
        self.db = db
        self.events = events
        self.config = config
        self.model = model

    # -- lifecycle --------------------------------------------------------

    def create(
        self,
        *,
        task_id: str,
        run_id: str,
        action: str,
        tool: str,
        arguments: dict[str, Any],
        risk: str,
        reason: str | None = None,
        tool_call_id: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        if action not in APPROVAL_ACTIONS:
            raise ApprovalError(f"unknown approval action: {action}")
        approval_id = util.new_id("apr")
        ts = util.iso()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO approvals(id, task_id, run_id, tool, tool_call_id,"
                " arguments, risk, reason, status, created_at, expires_at, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    approval_id, task_id, run_id, tool, tool_call_id,
                    util.dumps(arguments), risk, reason, "pending", ts,
                    expires_at, util.dumps({"action": action}),
                ),
            )
        self.events.record(
            "approval.created", task_id=task_id, run_id=run_id,
            data={"approval_id": approval_id, "tool": tool, "risk": risk},
        )
        return self.get(approval_id)  # type: ignore[return-value]

    def get(self, approval_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)
        )
        if not row:
            return None
        out = rows_to_dicts([row])[0]
        out["arguments"] = util.loads(out.get("arguments") or "{}", {})
        out["meta"] = util.loads(out.get("meta") or "{}", {})
        return out

    def decide(
        self,
        approval_id: str,
        decision: str,
        *,
        decided_by: str,
    ) -> dict[str, Any]:
        """Approve/reject/cancel a pending approval. Explicit outcomes only.

        The transition is a single conditional UPDATE, so two concurrent
        decisions cannot both win.
        """
        if decision not in ("approve", "reject", "cancel"):
            raise ApprovalError(f"invalid decision: {decision}")
        status = {
            "approve": "approved", "reject": "rejected",
            "cancel": "cancelled",
        }[decision]
        now = util.iso()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if row is None:
                raise ApprovalError(f"no such approval: {approval_id}")
            approval = rows_to_dicts([row])[0]
            if approval["status"] != "pending":
                raise ApprovalError(
                    f"approval {approval_id} is already {approval['status']}"
                )
            if self._is_expired(approval):
                conn.execute(
                    "UPDATE approvals SET status='expired', decided_at=?,"
                    " decided_by='system' WHERE id=? AND status='pending'",
                    (now, approval_id),
                )
                expired = True
            else:
                cur = conn.execute(
                    "UPDATE approvals SET status=?, decided_at=?, decided_by=?"
                    " WHERE id=? AND status='pending'",
                    (status, now, decided_by, approval_id),
                )
                if cur.rowcount != 1:
                    raise ApprovalError(
                        f"approval {approval_id} was decided concurrently"
                    )
                expired = False
        if expired:
            self.events.record(
                "approval.expired", task_id=approval["task_id"],
                run_id=approval["run_id"],
                data={"approval_id": approval_id,
                      "note": "expired before decision"},
            )
            raise ApprovalError(f"approval {approval_id} expired")
        self.events.record(
            f"approval.{status}", task_id=approval["task_id"],
            run_id=approval["run_id"],
            data={"approval_id": approval_id, "decided_by": decided_by},
        )
        return self.get(approval_id)  # type: ignore[return-value]

    def claim_for_execution(self, approval_id: str) -> str | None:
        """Atomically claim the awaiting execution for an approved approval.

        Returns the execution id, or ``None`` when the approval is no longer
        approved or the execution is no longer awaiting. Claiming flips the
        row to ``in_flight`` *before* the handler runs, so a crash is visible
        as an unknown outcome rather than silently losing the attempt.
        """
        now = util.iso()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT status, run_id, task_id FROM approvals WHERE id=?",
                (approval_id,),
            ).fetchone()
            if row is None or row["status"] != "approved":
                return None
            execution = conn.execute(
                "SELECT id FROM tool_executions WHERE approval_id=? AND"
                " status='awaiting_approval' ORDER BY started_at LIMIT 1",
                (approval_id,),
            ).fetchone()
            if execution is None:
                return None
            updated = conn.execute(
                "UPDATE tool_executions SET status='in_flight', started_at=?"
                " WHERE id=? AND status='awaiting_approval'",
                (now, execution["id"]),
            )
            if updated.rowcount != 1:
                return None
            return execution["id"]

    def approved_awaiting_execution(self) -> list[dict[str, Any]]:
        """Approvals that were granted but never claimed (crash window)."""
        rows = self.db.query(
            "SELECT a.id FROM approvals a JOIN tool_executions e"
            " ON e.approval_id=a.id"
            " WHERE a.status='approved' AND e.status='awaiting_approval'"
            " ORDER BY a.created_at"
        )
        return [self.get(row["id"]) for row in rows]  # type: ignore[misc]

    def live_for_run(self, run_id: str) -> dict[str, Any] | None:
        """The pending/approved approval that still owns this run, if any."""
        row = self.db.query_one(
            "SELECT id FROM approvals WHERE run_id=? AND"
            " status IN ('pending','approved') ORDER BY created_at LIMIT 1",
            (run_id,),
        )
        return self.get(row["id"]) if row else None

    def cancel(
        self, approval_id: str, *, reason: str,
        decided_by: str = "system",
    ) -> bool:
        """Cancel a pending/approved approval that can no longer be used."""
        now = util.iso()
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status='cancelled', decided_at=?,"
                " decided_by=? WHERE id=? AND status IN ('pending','approved')",
                (now, decided_by, approval_id),
            )
            if cur.rowcount != 1:
                return False
        self.events.record(
            "approval.cancelled", data={
                "approval_id": approval_id, "decided_by": decided_by,
                "reason": reason,
            },
        )
        return True

    def mark_executed(self, approval_id: str) -> None:
        """Execution happened under this approval; keep who decided it."""
        self.db.execute(
            "UPDATE approvals SET status='executed' WHERE id=? AND"
            " status='approved'",
            (approval_id,),
        )
        self.events.record(
            "approval.executed", data={"approval_id": approval_id}
        )

    def _transition(
        self, approval_id: str, status: str, *, decided_by: str,
        note: str | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_by=? WHERE id=?",
            (status, util.iso(), decided_by, approval_id),
        )
        if note:
            self.events.record(
                "approval.note", data={"approval_id": approval_id, "note": note}
            )

    def _is_expired(self, approval: dict[str, Any]) -> bool:
        if not approval.get("expires_at"):
            return False
        return util.parse_iso(approval["expires_at"]) < util.now_utc()

    def expire_stale(self) -> int:
        rows = rows_to_dicts(self.db.query(
            "SELECT * FROM approvals WHERE status='pending' AND expires_at"
            " IS NOT NULL"
        ))
        count = 0
        for row in rows:
            approval = self.get(row["id"])
            if approval and self._is_expired(approval):
                self._transition(row["id"], "expired", decided_by="system",
                                  note="expired")
                self.events.record(
                    "approval.expired", data={"approval_id": row["id"]}
                )
                count += 1
        return count

    # -- queries ----------------------------------------------------------

    def pending(self, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id:
            rows = self.db.query(
                "SELECT * FROM approvals WHERE status='pending' AND task_id=?"
                " ORDER BY created_at DESC",
                (task_id,),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM approvals WHERE status='pending'"
                " ORDER BY created_at DESC"
            )
        return [self.get(r["id"]) for r in rows]  # type: ignore[misc]

    def pending_for_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT id FROM approvals WHERE status='pending' AND run_id=?"
            " ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        )
        return self.get(row["id"]) if row else None

    # -- quick confirmation -----------------------------------------------

    def maybe_quick_confirm(
        self, text: str, pending: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Try to interpret a short user reply as a decision on one pending
        approval. Returns a decision dict or None (normal message flow).

        Only ever classifies; it cannot execute anything. With more than one
        pending approval the reply is not auto-applied — the assistant asks
        which action is meant.
        """
        stripped = (text or "").strip()
        if not pending or len(stripped) > 80 or "\n" in stripped:
            return None
        lowered = stripped.lower().strip(" .!?")
        if len(lowered.split()) > 10:
            return None
        if len(pending) > 1:
            return {"kind": "ambiguous_multiple", "pending": pending}
        approval = pending[0]
        # Deterministic fast path for unambiguous keywords.
        if lowered in _AFFIRMATIVE:
            return {"kind": "decision", "decision": "approve",
                    "approval_id": approval["id"], "via": "keyword"}
        if lowered in _NEGATIVE:
            return {"kind": "decision", "decision": "reject",
                    "approval_id": approval["id"], "via": "keyword"}
        # Model classification for short, non-obvious replies. Constrained:
        # classify only, never executes, never widens scope.
        try:
            label = self._classify(stripped, approval)
        except (ModelUnavailable, ModelResponseError):
            return None  # treat as a normal message; never guess
        if label == "affirmative":
            return {"kind": "decision", "decision": "approve",
                    "approval_id": approval["id"], "via": "model"}
        if label == "negative":
            return {"kind": "decision", "decision": "reject",
                    "approval_id": approval["id"], "via": "model"}
        return {"kind": "ambiguous", "approval_id": approval["id"]}

    def _classify(self, text: str, approval: dict[str, Any]) -> str:
        """Tiny constrained model call: affirmative | negative | ambiguous."""
        prompt = (
            "You classify a user's short reply to a pending approval request. "
            "The requested action is:\n"
            f"{approval['tool']} with arguments "
            f"{util.dumps(approval['arguments'])[:400]}\n"
            "The user replied:\n"
            f"\"{text}\"\n"
            "Answer with exactly one word: affirmative, negative, or ambiguous. "
            "Use ambiguous unless the reply clearly approves or clearly "
            "refuses this specific request."
        )
        reply = self.model.chat(
            [{"role": "user", "content": prompt}], tools=[], max_tokens=200
        )
        label = (reply.content or "").strip().lower()
        for candidate in ("affirmative", "negative", "ambiguous"):
            if candidate in label:
                return candidate
        return "ambiguous"

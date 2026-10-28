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

import datetime as dt
import re
from typing import Any

from . import util
from .db import Database, rows_to_dicts
from .model import Model, ModelUnavailable, ModelResponseError
from .store import Events

APPROVAL_ACTIONS = ("tool", "create_domain")

# Deterministic, conservative phrase lists. These are *exact* normalized
# phrases, never substrings: "not affirmative" must not read as approval.
_AFFIRMATIVE = {
    "yes", "y", "yeah", "yep", "yup", "ok", "okay", "k", "sure", "approve",
    "approved", "confirm", "confirmed", "accept", "accepted", "allow",
    "allowed", "permit", "permitted", "grant", "granted", "do it", "go",
    "go ahead", "proceed", "continue", "affirmative", "please do",
    "please proceed", "please approve", "please confirm", "i approve",
    "i confirm", "approve it", "do that", "that's fine", "that is fine",
    "fine", "sounds good", "yes please", "yes do it", "yes go ahead",
    "ok do it", "okay do it", "sure do it", "no problem", "no worries",
}
_NEGATIVE = {
    "no", "n", "nope", "nah", "negative", "reject", "rejected", "deny",
    "denied", "cancel", "cancelled", "canceled", "stop", "don't", "dont",
    "do not", "never", "refuse", "refused", "block", "block it",
    "absolutely not", "no thanks", "no thank you", "don't do it",
    "dont do it", "do not do it", "no way", "not now", "leave it",
}

_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"',
                              "\u201d": '"'})


def normalize_reply(text: str) -> str:
    """Lowercase, unify quotes, drop punctuation, collapse whitespace."""
    lowered = (text or "").translate(_APOSTROPHES).strip().lower()
    lowered = re.sub(r"[^a-z0-9'\s]+", " ", lowered)
    return " ".join(lowered.split())


_APPROVE_WORDS = {
    "approve", "approved", "yes", "y", "ok", "okay", "confirm", "confirmed",
    "accept", "allow", "permit", "proceed", "sure",
}
_REJECT_WORDS = {
    "reject", "rejected", "deny", "denied", "no", "n", "cancel", "cancelled",
    "canceled", "stop", "never", "refuse", "refused",
}


def _leading_decision(normalized: str) -> str | None:
    """Classify a decision from the first word, for multi-approval replies.

    Used only when the reply also names exactly one pending action; the
    deterministic single-approval path still requires an exact phrase.
    """
    words = normalized.split()
    if not words:
        return None
    if words[0] in _APPROVE_WORDS:
        return "approve"
    if words[0] in _REJECT_WORDS:
        return "reject"
    return None



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
        if expires_at is None:
            ttl = float(self.config.get("approvals.expires_after_s", 0) or 0)
            if ttl > 0:
                expires_at = util.iso(util.now_utc() + dt.timedelta(seconds=ttl))
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

    def describe(self, approval: dict[str, Any]) -> str:
        """One-line human description used in questions and the UI."""
        action = (approval.get("meta") or {}).get("action", "tool")
        arguments = approval.get("arguments") or {}
        if action == "create_domain":
            name = arguments.get("name") or "a new domain"
            return f"create the domain '{name}'"
        detail = util.dumps(arguments)
        if len(detail) > 120:
            detail = detail[:117] + "…"
        return f"run {approval.get('tool')} with {detail}"

    def dedupe_pending(
        self, actions: tuple[str, ...] = ("create_domain",)
    ) -> int:
        """Cancel duplicate pending approvals for the same action+arguments.

        Newest wins (``pending`` is ordered newest first) so the approval a
        user is most likely looking at stays actionable. Only the listed
        virtual actions are deduped by default; external tool approvals can
        legitimately repeat with identical arguments.
        """
        seen: dict[tuple[str, str, str], str] = {}
        cancelled = 0
        for approval in self.pending():
            action = (approval.get("meta") or {}).get("action", "tool")
            if action not in actions:
                continue
            key = (action, str(approval.get("tool")), util.dumps(
                approval.get("arguments") or {}
            ))
            keeper = seen.get(key)
            if keeper is None:
                seen[key] = approval["id"]
                continue
            if self.cancel(
                approval["id"],
                reason=f"duplicate of {keeper}",
            ):
                cancelled += 1
        return cancelled

    # -- quick confirmation -----------------------------------------------

    def maybe_quick_confirm(
        self, text: str, pending: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Try to interpret a short user reply as a decision on one pending
        approval. Returns a decision dict or None (normal message flow).

        Only ever classifies; it cannot execute anything. A reply is applied
        only when the target is unambiguous: one pending approval, or a reply
        that names exactly one of several pending actions. Everything else
        returns a question, never a decision.
        """
        stripped = (text or "").strip()
        if not pending or len(stripped) > 120 or "\n" in stripped:
            return None
        normalized = normalize_reply(stripped)
        if not normalized or len(normalized.split()) > 12:
            return None
        if len(pending) > 1:
            return self._disambiguate(stripped, normalized, pending)
        approval = pending[0]
        # Deterministic fast path for unambiguous phrases.
        if normalized in _AFFIRMATIVE:
            return {"kind": "decision", "decision": "approve",
                    "approval_id": approval["id"], "via": "keyword"}
        if normalized in _NEGATIVE:
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
        return {"kind": "ambiguous", "approval_id": approval["id"],
                "summary": self.describe(approval)}

    def _disambiguate(
        self, text: str, normalized: str, pending: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Resolve a decision only if the reply names exactly one action."""
        words = set(normalized.split())
        named: list[dict[str, Any]] = []
        for approval in pending:
            tool = normalize_reply(str(approval.get("tool") or ""))
            name = normalize_reply(
                str((approval.get("arguments") or {}).get("name") or "")
            )
            tokens = set(tool.split()) | set(name.split())
            if tokens and tokens & words:
                named.append(approval)
        decision = _leading_decision(normalized)
        if decision and len(named) == 1:
            return {"kind": "decision", "decision": decision,
                    "approval_id": named[0]["id"], "via": "keyword"}
        return {
            "kind": "ambiguous_multiple",
            "pending": pending,
            "named": [a["id"] for a in named],
            "summaries": [self.describe(a) for a in pending],
        }

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
            [{"role": "user", "content": prompt}],
            tools=[],
            max_tokens=int(
                self.config.get("approvals.classifier_max_tokens", 16)
            ),
            thinking=bool(
                self.config.get("approvals.classifier_thinking", False)
            ),
            recovery=False,
        )
        # Parse exactly one leading label. Substring matching would read
        # "not affirmative" as approval.
        words = re.findall(r"[a-z]+", (reply.content or "").lower())
        first = words[0] if words else ""
        if first in ("affirmative", "yes", "approve", "approved"):
            return "affirmative"
        if first in ("negative", "no", "reject", "rejected"):
            return "negative"
        return "ambiguous"

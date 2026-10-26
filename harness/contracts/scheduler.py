from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class TickItem:
    """One deterministic finding or action from a scheduler tick."""

    kind: str
    action: str
    task_id: str | None = None
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class TickReport:
    """What one heartbeat found and did.

    A tick is deterministic: it scans persisted tasks, decisions, and attempt
    evidence, performs state transitions, and at most runs a bounded number
    of ready tasks through the normal loop. The model is only invoked through
    that loop (or by consolidation extraction), never by the scan itself.
    """

    ts: str
    items: list[TickItem] = field(default_factory=list)
    ran: int = 0
    model_used: bool = False
    enabled: bool = True

    @property
    def summary(self) -> str:
        if not self.items and not self.ran:
            return "nothing to do"
        parts = [f"{len(self.items)} finding(s)"] if self.items else []
        if self.ran:
            parts.append(f"ran {self.ran} task(s)")
        return ", ".join(parts)


@dataclass
class WorkItem:
    """One task rendered for the honest-status view."""

    id: str
    goal: str
    state: str
    block_reason: str | None = None
    priority: int = 0
    due_at: str | None = None
    defer_until: str | None = None
    domain_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    # What the task is waiting on, derived from state + block reason:
    # "user", "approval", "dependency", "cooldown", "limit", "defer",
    # "unknown_outcome", or None when eligible/terminal.
    waiting_on: str | None = None
    # Execution evidence summary; attempts themselves stay in the attempt store.
    attempts: int = 0
    last_attempt_status: str | None = None
    last_attempt_operation: str | None = None
    succeeded_execution: bool = False
    delivered_response: bool = False
    progress: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DecisionItem:
    """One open decision rendered for the honest-status view."""

    id: str
    kind: str
    summary: str
    risk: str
    status: str
    task_id: str | None = None
    created_at: str = ""


@dataclass
class WorkReport:
    """The answer to: what am I working on, what is waiting, what failed."""

    ts: str
    working: list[WorkItem] = field(default_factory=list)
    ready: list[WorkItem] = field(default_factory=list)
    waiting: list[WorkItem] = field(default_factory=list)
    blocked: list[WorkItem] = field(default_factory=list)
    deferred: list[WorkItem] = field(default_factory=list)
    due: list[WorkItem] = field(default_factory=list)
    failed: list[WorkItem] = field(default_factory=list)
    completed: list[WorkItem] = field(default_factory=list)
    needs_approval: list[WorkItem] = field(default_factory=list)
    open_decisions: list[DecisionItem] = field(default_factory=list)
    next_hint: str = ""

    def summary(self) -> str:
        lines = [f"Working on: {self._line(self.working) or 'nothing right now'}"]
        lines.append(f"Waiting on you: {self._line(self.waiting) or 'nothing'}")
        lines.append(f"Needs approval: {self._line(self.needs_approval) or 'nothing'}")
        blocked = self._line(self.blocked)
        lines.append(f"Blocked: {blocked or 'nothing'}")
        deferred = self._line(self.deferred)
        lines.append(f"Deferred: {deferred or 'nothing'}")
        due = self._line(self.due)
        lines.append(f"Due: {due or 'nothing'}")
        lines.append(f"Ready next: {self._line(self.ready) or 'nothing'}")
        failed = self._line(self.failed)
        lines.append(f"Failed: {failed or 'nothing'}")
        done = self._line(self.completed)
        lines.append(f"Completed recently: {done or 'nothing'}")
        if self.next_hint:
            lines.append(f"Next: {self.next_hint}")
        return "\n".join(lines)

    @staticmethod
    def _line(items: list[WorkItem]) -> str:
        rendered = []
        for item in items:
            text = f"{item.id} {item.goal}"
            if item.block_reason:
                text += f" [{item.block_reason}]"
            rendered.append(text)
        return "; ".join(rendered)


class WorkScheduler(Protocol):
    """Deterministic heartbeat plus the honest-status read model.

    The scheduler never dispatches capabilities itself. It transitions task
    state from persisted evidence and, when work is eligible, hands it to the
    normal interaction loop through the turn service, which enforces the same
    confirmation and permission gates as a user turn.
    """

    def reconcile(self) -> TickReport: ...
    def tick(self) -> TickReport: ...
    def report(self) -> WorkReport: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...

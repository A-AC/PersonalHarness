from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from harness.constants import (
    BlockReason,
    DecisionStatus,
    ExecutionStatus,
    PermissionLevel,
    TaskState,
)
from harness.contracts.capabilities import CapabilityExecutor
from harness.contracts.decisions import Decision, DecisionStore
from harness.contracts.events import Event, EventLog
from harness.contracts.loop import TurnResult
from harness.contracts.scheduler import DecisionItem, TickItem, TickReport, WorkItem, WorkReport
from harness.contracts.settings import SettingsService
from harness.contracts.tasks import Task, TaskManager
from harness.storage.atomic import new_id, now_ts


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(stamp: str | None, now: datetime) -> float | None:
    parsed = _parse_ts(stamp)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed).total_seconds())


class WorkScheduler:
    """Lightweight persistent heartbeat over the existing task system.

    The scheduler owns no execution path of its own. A tick is a deterministic
    scan of persisted tasks, decisions, and attempt evidence that:

    1. reconciles interrupted attempts whose outcome is unknown (asks the
       user instead of guessing or blindly repeating the action),
    2. re-checks dependencies and releases or fails dependents accordingly,
    3. releases deferred tasks whose window has passed (unless an open
       decision still holds them),
    4. requeues recoverable work after a cooldown, bounded by a retry
       budget, and marks the remainder as failed,
    5. surfaces overdue tasks once,
    6. runs scheduled consolidation when it is due,
    7. runs at most ``scheduler.max_tasks_per_tick`` ready tasks through the
       normal turn path (model + confirmation gates included).

    Nothing here dispatches a capability directly. The model is invoked only
    through the runner (the turn service), and only when there is real work.
    """

    def __init__(
        self,
        *,
        settings: SettingsService,
        tasks: TaskManager,
        decisions: DecisionStore,
        capabilities: CapabilityExecutor,
        events: EventLog,
        consolidation: Any | None = None,
        runner: Callable[[int], TurnResult] | None = None,
        learning_review: Any | None = None,
    ):
        self._settings = settings
        self._tasks = tasks
        self._decisions = decisions
        self._capabilities = capabilities
        self._events = events
        self._consolidation = consolidation
        self._runner = runner
        self._learning_review = learning_review
        self._lock = threading.Lock()
        self._blocked: str | None = None
        self._epoch = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle
    @contextmanager
    def exclusive(self, reason: str = "reset") -> Iterator[None]:
        """Quiesce the heartbeat for destructive maintenance (reset)."""
        with self._lock:
            self._epoch += 1
            self._blocked = reason
            try:
                yield
            finally:
                self._blocked = None

    def start(self) -> None:
        """Run the heartbeat on a daemon thread while the app is serving."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="harness-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = max(5, int(self._settings.get("scheduler.interval_s")))
            self._stop.wait(interval)
            if self._stop.is_set():
                return
            try:
                self.tick()
            except Exception as exc:  # a failed tick must not kill the thread
                self._safe_event(
                    Event(
                        id=new_id("evt"),
                        ts=now_ts(),
                        actor="harness",
                        operation="scheduler.error",
                        target="scheduler",
                        result="error",
                        data={"error": f"{type(exc).__name__}: {exc}"},
                    )
                )

    # ----------------------------------------------------------------- tick
    def reconcile(self) -> TickReport:
        """Deterministic recovery pass; safe to run at startup.

        No model calls, no ready-work execution: this only repairs state from
        persisted evidence so the system never mistakes an interrupted or
        unknown-outcome task for runnable work.
        """
        report = TickReport(ts=now_ts(), enabled=True)
        now = datetime.now(timezone.utc)
        self._reconcile_unknown_outcomes(report, now)
        self._recheck_dependencies(report)
        self._release_deferred(report, now)
        self._emit_tick_event(report, trigger="reconcile")
        return report

    def tick(self) -> TickReport:
        epoch = self._epoch
        with self._lock:
            if self._blocked or epoch != self._epoch:
                return TickReport(
                    ts=now_ts(),
                    enabled=False,
                    items=[TickItem(kind="tick", action="skipped", reason=f"harness is {self._blocked or 'resetting'}")],
                )
            if not bool(self._settings.get("scheduler.enabled")):
                return TickReport(ts=now_ts(), enabled=False)
            report = self.reconcile()
            now = datetime.now(timezone.utc)
            self._requeue_recoverable(report, now)
            self._notify_due(report, now)
            self._scheduled_consolidation(report, now)
            self._scheduled_learning_review(report, now)
            self._run_ready(report)
            self._emit_tick_event(report, trigger="tick")
            return report

    # ------------------------------------------------------- reconcile steps
    def _reconcile_unknown_outcomes(self, report: TickReport, now: datetime) -> None:
        for task in self._tasks.list(states=[TaskState.BLOCKED]):
            if task.block_reason != BlockReason.INTERRUPTED:
                continue
            attempts = self._capabilities.last_attempts(task.id)
            last = attempts[-1] if attempts else None
            in_flight = bool(
                last is not None
                and (last.evidence or {}).get("confirmation") == "in_flight"
            )
            if not in_flight:
                continue
            # The process died mid-dispatch. Neither success nor failure may be
            # assumed: block the task on an explicit user reconciliation.
            if self._open_decision_for(task.id) is not None:
                continue
            self._tasks.transition(task.id, TaskState.BLOCKED, reason=BlockReason.UNKNOWN_OUTCOME)
            self._create_reconciliation_decision(task, last)
            self._tasks.record_checkpoint(
                task.id,
                f"Attempt {last.operation_id} ({last.capability}.{last.operation}) was left in flight; "
                "outcome unknown, waiting for your confirmation before any retry.",
                {"operation_id": last.operation_id, "status": ExecutionStatus.UNKNOWN.value},
            )
            report.items.append(
                TickItem(
                    kind="unknown_outcome",
                    action="blocked_for_reconciliation",
                    task_id=task.id,
                    reason="attempt left in flight by an interruption",
                    detail={"operation_id": last.operation_id},
                )
            )

    def _recheck_dependencies(self, report: TickReport) -> None:
        for task in self._tasks.list(states=[TaskState.PENDING, TaskState.BLOCKED]):
            if not task.depends_on or task.block_reason != BlockReason.DEPENDENCY:
                continue
            failed: list[str] = []
            remaining: list[str] = []
            for dep in task.depends_on:
                try:
                    state = self._tasks.get(dep).state
                except KeyError:
                    state = TaskState.CANCELLED
                if state == TaskState.COMPLETED:
                    continue
                if state in {TaskState.FAILED, TaskState.CANCELLED}:
                    failed.append(f"{dep}:{state}")
                else:
                    remaining.append(dep)
            if failed:
                self._tasks.transition(task.id, TaskState.FAILED, reason="dependency_failed")
                self._tasks.record_checkpoint(
                    task.id,
                    f"Cannot proceed: dependency status {', '.join(failed)}.",
                    {"failed_dependencies": failed},
                )
                report.items.append(
                    TickItem(
                        kind="dependency",
                        action="failed",
                        task_id=task.id,
                        reason="dependency failed or was cancelled",
                        detail={"dependencies": failed},
                    )
                )
            elif not remaining:
                self._tasks.transition(task.id, TaskState.READY, reason=None)
                report.items.append(
                    TickItem(
                        kind="dependency",
                        action="released",
                        task_id=task.id,
                        reason="all dependencies completed",
                    )
                )

    def _release_deferred(self, report: TickReport, now: datetime) -> None:
        for task in self._tasks.list(states=[TaskState.DEFERRED]):
            if not task.defer_until:
                continue
            until = _parse_ts(task.defer_until)
            if until is None or until > now:
                continue
            if self._open_decision_for(task.id) is not None:
                # A deferred decision still holds the work; only the user's
                # answer (approve/reject) resumes it, not the clock.
                continue
            self._tasks.transition(task.id, TaskState.READY, reason=None)
            report.items.append(
                TickItem(
                    kind="deferred",
                    action="released",
                    task_id=task.id,
                    reason=f"defer window passed ({task.defer_until})",
                )
            )

    # ----------------------------------------------------------- tick steps
    def _requeue_recoverable(self, report: TickReport, now: datetime) -> None:
        cooldown = float(self._settings.get("scheduler.retry_cooldown_s"))
        max_retries = int(self._settings.get("scheduler.max_task_retries"))
        transient = {
            BlockReason.INTERRUPTED,
            BlockReason.MODEL_UNAVAILABLE,
            BlockReason.CAPABILITY_UNAVAILABLE,
        }
        for task in self._tasks.list(states=[TaskState.BLOCKED, TaskState.FAILED]):
            if task.state == TaskState.FAILED:
                if not self._retryable_failure(task):
                    continue
            elif task.block_reason not in transient:
                # awaiting_user / awaiting_confirmation / dependency /
                # unknown_outcome / limit are waiting on someone else, and
                # the reconcile pass owns dependency and unknown outcomes.
                continue
            if self._open_decision_for(task.id) is not None:
                continue
            age = _age_seconds(task.updated_at, now)
            if age is None or age < cooldown:
                continue
            if task.resume_count >= max_retries:
                self._mark_retry_exhausted(task, report)
                continue
            self._tasks.resume(task.id, reason="scheduler requeued recoverable work")
            report.items.append(
                TickItem(
                    kind="requeue",
                    action="resumed",
                    task_id=task.id,
                    reason=f"stopped work requeued after cooldown ({task.resume_count + 1}/{max_retries} resume budget)",
                )
            )

    def _retryable_failure(self, task: Task) -> bool:
        """Evidence check: the last attempt's error category must be retryable
        for that operation, and its outcome must be known (a final, non-
        succeeded status). Unknown outcomes go through reconciliation instead."""
        attempts = self._capabilities.last_attempts(task.id)
        last = attempts[-1] if attempts else None
        if last is None or last.status == ExecutionStatus.UNKNOWN:
            return False
        if last.status == ExecutionStatus.SUCCEEDED:
            # The work stopped after a success (for example an interruption
            # right after dispatch); continuing is safe and bounded.
            return True
        try:
            manifest = self._capabilities.get(last.capability)
        except KeyError:
            return False
        for op in manifest.operations:
            if op.name == last.operation:
                return bool(last.error_category) and last.error_category in set(op.retryable_error_categories)
        return False

    def _mark_retry_exhausted(self, task: Task, report: TickReport) -> None:
        already = any((item.get("data") or {}).get("kind") == "retry_limit" for item in task.progress)
        try:
            self._tasks.transition(task.id, TaskState.FAILED, reason=BlockReason.RETRY_LIMIT)
        except ValueError:
            pass
        if not already:
            self._tasks.record_checkpoint(
                task.id,
                f"Retry budget exhausted ({task.resume_count} resumes); the scheduler will not requeue this again.",
                {"kind": "retry_limit", "resume_count": task.resume_count},
            )
            self._safe_event(
                Event(
                    id=new_id("evt"),
                    ts=now_ts(),
                    actor="harness",
                    operation="task.retry_exhausted",
                    target=task.id,
                    result=TaskState.FAILED.value,
                    data={"resume_count": task.resume_count},
                )
            )
        report.items.append(
            TickItem(
                kind="requeue",
                action="retry_limit",
                task_id=task.id,
                reason=f"retry budget exhausted after {task.resume_count} resumes",
            )
        )

    def _notify_due(self, report: TickReport, now: datetime) -> None:
        for task in self._tasks.list():
            if task.state in {TaskState.COMPLETED, TaskState.CANCELLED}:
                continue
            if not task.due_at or _parse_ts(task.due_at) is None or _parse_ts(task.due_at) > now:
                continue
            already = any(
                (item.get("data") or {}).get("kind") == "due_notified"
                and (item.get("data") or {}).get("due_at") == task.due_at
                for item in task.progress
            )
            if already:
                continue
            self._tasks.record_checkpoint(
                task.id,
                f"Task is due (deadline {task.due_at}) and still {task.state}.",
                {"kind": "due_notified", "due_at": task.due_at},
            )
            self._safe_event(
                Event(
                    id=new_id("evt"),
                    ts=now_ts(),
                    actor="harness",
                    operation="task.due",
                    target=task.id,
                    result=task.state,
                    data={"due_at": task.due_at},
                )
            )
            report.items.append(
                TickItem(
                    kind="due",
                    action="notified",
                    task_id=task.id,
                    reason=f"deadline {task.due_at} passed while {task.state}",
                )
            )

    def _scheduled_consolidation(self, report: TickReport, now: datetime) -> None:
        if self._consolidation is None:
            return
        if not bool(self._settings.get("consolidation.schedule_enabled")):
            return
        interval = float(self._settings.get("consolidation.schedule_interval_s"))
        last: Any | None = None
        try:
            runs = self._consolidation.list(50)
        except Exception:
            runs = []
        for run in runs:
            if last is None or str(run.started_at) > str(last.started_at):
                last = run
        if last is not None:
            age = _age_seconds(str(last.started_at), now)
            if age is not None and age < interval:
                return
        try:
            run = self._consolidation.run(trigger="schedule")
            # Consolidation only invokes the model when extraction is enabled.
            report.model_used = report.model_used or bool(
                self._settings.get("consolidation.extract_enabled")
            )
            report.items.append(
                TickItem(
                    kind="consolidation",
                    action="ran",
                    reason=f"scheduled consolidation run {run.id} finished {run.status}",
                    detail={"run_id": run.id, "status": run.status},
                )
            )
        except Exception as exc:
            report.items.append(
                TickItem(
                    kind="consolidation",
                    action="failed",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )

    def _scheduled_learning_review(self, report: TickReport, now: datetime) -> None:
        """Run the deterministic learning review when it is due.

        The review scans persisted evidence and creates staged candidates at
        most; it never invokes a model, so ``model_used`` stays false. A
        review with no meaningful new evidence changes nothing.
        """
        if self._learning_review is None:
            return
        if not bool(self._settings.get("learning.review_schedule_enabled")):
            return
        interval = float(self._settings.get("learning.review_interval_s"))
        last: Any | None = None
        try:
            events = self._events.query(operation="learning.review", limit=1)
            last = events[-1] if events else None
        except Exception:
            last = None
        if last is not None:
            age = _age_seconds(str(last.ts), now)
            if age is not None and age < interval:
                return
        try:
            result = self._learning_review.review(trigger="schedule")
        except Exception as exc:
            report.items.append(
                TickItem(
                    kind="learning_review",
                    action="failed",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            return
        if result.items or result.changed:
            report.items.append(
                TickItem(
                    kind="learning_review",
                    action="ran",
                    reason=(
                        f"learning review created {len(result.candidates)} candidate(s), "
                        f"{len(result.items)} finding(s)"
                    ),
                    detail={"candidates": list(result.candidates)},
                )
            )

    def _run_ready(self, report: TickReport) -> None:
        if self._runner is None:
            return
        limit = int(self._settings.get("scheduler.max_tasks_per_tick"))
        if limit <= 0:
            return
        result = self._runner(limit)
        report.ran = len(result.tasks)
        if result.messages or result.error:
            report.model_used = True
        if result.error:
            report.items.append(
                TickItem(kind="run_ready", action="failed", reason=result.error)
            )

    # ------------------------------------------------------------- decisions
    def _open_decision_for(self, task_id: str) -> Decision | None:
        try:
            return self._decisions.find_for_task(task_id, open_only=True)
        except KeyError:
            return None

    def _create_reconciliation_decision(self, task: Task, attempt: Any) -> None:
        risk = "unknown"
        try:
            manifest = self._capabilities.get(attempt.capability)
            for op in manifest.operations:
                if op.name == attempt.operation:
                    risk = str(op.permission)
                    break
        except KeyError:
            pass
        if risk == PermissionLevel.WRITE.value or risk == PermissionLevel.DESTRUCTIVE.value:
            risk = "write" if risk == PermissionLevel.WRITE.value else "destructive"
        label = f"{attempt.capability}.{attempt.operation}"
        ts = now_ts()
        decision = Decision(
            id=new_id("dec"),
            kind="reconciliation",
            status=DecisionStatus.PENDING.value,
            created_at=ts,
            updated_at=ts,
            prompt=(
                f"An earlier {label} call was interrupted, so I can't tell whether it took effect. "
                "Did it complete?"
            ),
            summary=f"Confirm whether the interrupted {label} completed",
            proposal={
                "type": "reconciliation",
                "task_id": task.id,
                "operation_id": attempt.operation_id,
                "capability": attempt.capability,
                "operation": attempt.operation,
                "started_at": (attempt.evidence or {}).get("started_at"),
            },
            scope={"task_ids": [task.id], "risk": risk},
            risk=risk,
            task_id=task.id,
            source="harness",
        )
        self._decisions.create(decision)

    # ---------------------------------------------------------------- events
    def _emit_tick_event(self, report: TickReport, *, trigger: str) -> None:
        if not report.items and not report.ran:
            return
        self._safe_event(
            Event(
                id=new_id("evt"),
                ts=now_ts(),
                actor="harness",
                operation="scheduler.tick",
                target="scheduler",
                result=trigger,
                data={
                    "items": [
                        {
                            "kind": item.kind,
                            "action": item.action,
                            "task_id": item.task_id,
                            "reason": item.reason,
                        }
                        for item in report.items
                    ],
                    "ran": report.ran,
                    "model_used": report.model_used,
                },
            )
        )

    def _safe_event(self, event: Event) -> None:
        try:
            self._events.append(event)
        except Exception:
            pass

    # ------------------------------------------------------------- read model
    def report(self) -> WorkReport:
        tasks = self._tasks.list()
        attempts_by_task = self._capabilities.attempts_by_task()
        decisions = self._decisions.pending(include_deferred=True)
        # An accepted plan that was never fully executed (for example an
        # interruption mid-plan) is not open for a new answer, but it is
        # unfinished accepted work and must stay visible.
        unfinished_accepted = [
            d
            for d in self._decisions.list(status=DecisionStatus.APPROVED.value)
            if not d.consumed_at
        ]
        open_by_task: dict[str, list[Decision]] = {}
        for decision in [*decisions, *unfinished_accepted]:
            for task_id in decision.task_ids:
                open_by_task.setdefault(task_id, []).append(decision)

        def item(task: Task, waiting_on: str | None = None) -> WorkItem:
            attempts = attempts_by_task.get(task.id, [])
            last = attempts[-1] if attempts else None
            return WorkItem(
                id=task.id,
                goal=task.goal,
                state=task.state,
                block_reason=task.block_reason,
                priority=task.priority,
                due_at=task.due_at,
                defer_until=task.defer_until,
                domain_id=task.domain_id,
                created_at=task.created_at,
                updated_at=task.updated_at,
                waiting_on=waiting_on,
                attempts=len(attempts),
                last_attempt_status=str(last.status) if last else None,
                last_attempt_operation=f"{last.capability}.{last.operation}" if last else None,
                succeeded_execution=task.succeeded_execution,
                delivered_response=task.delivered_response,
                progress=list(task.progress),
            )

        def waiting_reason(task: Task) -> str | None:
            if task.state == TaskState.DEFERRED:
                return "defer"
            if task.state != TaskState.BLOCKED:
                return None
            return {
                BlockReason.AWAITING_USER: "user",
                BlockReason.AWAITING_CONFIRMATION: "approval",
                BlockReason.UNKNOWN_OUTCOME: "user",
                BlockReason.DEPENDENCY: "dependency",
                BlockReason.LIMIT: "limit",
                BlockReason.MODEL_UNAVAILABLE: "cooldown",
                BlockReason.CAPABILITY_UNAVAILABLE: "cooldown",
                BlockReason.INTERRUPTED: "cooldown",
            }.get(task.block_reason)

        now = datetime.now(timezone.utc)
        working = [item(t) for t in tasks if t.state == TaskState.RUNNING]
        ready = sorted(
            [item(t) for t in tasks if t.state == TaskState.READY],
            key=lambda w: (-w.priority, w.updated_at),
        )
        waiting = [
            item(t, waiting_reason(t))
            for t in tasks
            if t.state == TaskState.BLOCKED
            and t.block_reason in {BlockReason.AWAITING_USER, BlockReason.AWAITING_CONFIRMATION, BlockReason.UNKNOWN_OUTCOME}
        ]
        needs_approval = [
            item(t, "approval")
            for t in tasks
            if t.id in open_by_task or t.block_reason == BlockReason.AWAITING_CONFIRMATION
        ]
        blocked = [
            item(t, waiting_reason(t))
            for t in tasks
            if t.state == TaskState.BLOCKED
            and t.block_reason not in {BlockReason.AWAITING_USER, BlockReason.AWAITING_CONFIRMATION, BlockReason.UNKNOWN_OUTCOME}
        ]
        deferred = [item(t, "defer") for t in tasks if t.state == TaskState.DEFERRED]
        failed = sorted(
            [item(t) for t in tasks if t.state == TaskState.FAILED],
            key=lambda w: w.updated_at,
            reverse=True,
        )
        completed = sorted(
            [item(t) for t in tasks if t.state == TaskState.COMPLETED],
            key=lambda w: w.updated_at,
            reverse=True,
        )
        limit = int(self._settings.get("ui.page_limit"))
        due = [
            item(t)
            for t in tasks
            if t.state not in {TaskState.COMPLETED, TaskState.CANCELLED}
            and t.due_at
            and _parse_ts(t.due_at) is not None
            and _parse_ts(t.due_at) <= now
        ]
        return WorkReport(
            ts=now_ts(),
            working=working,
            ready=ready,
            waiting=waiting,
            blocked=blocked,
            deferred=deferred,
            due=due,
            failed=failed[:limit],
            completed=completed[:limit],
            needs_approval=needs_approval,
            open_decisions=[
                DecisionItem(
                    id=d.id,
                    kind=d.kind,
                    summary=d.summary,
                    risk=d.risk,
                    status=d.status,
                    task_id=d.task_id,
                    created_at=d.created_at,
                )
                for d in [*decisions, *unfinished_accepted]
            ],
            next_hint=self._next_hint(working, ready, waiting, needs_approval, deferred),
        )

    def _next_hint(
        self,
        working: list[WorkItem],
        ready: list[WorkItem],
        waiting: list[WorkItem],
        needs_approval: list[WorkItem],
        deferred: list[WorkItem],
    ) -> str:
        if working:
            return f"finish {working[0].id} ({working[0].goal})"
        if needs_approval:
            return f"wait for your approval on {needs_approval[0].id} ({needs_approval[0].goal})"
        if waiting:
            return f"wait for your answer on {waiting[0].id} ({waiting[0].goal})"
        if ready:
            runnable = bool(self._settings.get("scheduler.enabled")) and int(self._settings.get("scheduler.max_tasks_per_tick")) > 0
            verb = "run" if runnable else "run when you ask"
            return f"{verb} {ready[0].id} ({ready[0].goal})"
        if deferred:
            return f"hold {deferred[0].id} until its defer window passes"
        return "nothing scheduled"

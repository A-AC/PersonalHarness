from __future__ import annotations

import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from harness.app import build_harness
from harness.constants import (
    BlockReason,
    DecisionStatus,
    ExecutionStatus,
    TaskState,
)
from harness.contracts.capabilities import ExecuteRequest
from harness.contracts.tasks import TaskSpec
from harness.model.scripted import ScriptedAdapter
from harness.storage.atomic import atomic_write_json, now_ts
from tests.conftest import act, ask, plan, respond, write_settings


def past_ts(seconds: float = 60.0) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(seconds=seconds))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def future_ts(seconds: float = 3600.0) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


COOLDOWN_ZERO = {"scheduler": {"retry_cooldown_s": 0.0}}
# Cooldown zero plus no autonomous execution: ticks only repair state.
SCAN_ONLY = {"scheduler": {"retry_cooldown_s": 0.0, "max_tasks_per_tick": 0}}


# --------------------------------------------------------------- work model


def test_defer_and_resume_transitions(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="deferred work"))
    h.tasks.defer(task.id, future_ts(7200), reason="user said later")
    task = h.tasks.get(task.id)
    assert task.state == TaskState.DEFERRED
    assert task.defer_until
    assert task.block_reason == "user said later"

    # Re-deferring extends the window without changing state.
    h.tasks.defer(task.id, future_ts(9999), reason="user said much later")
    task = h.tasks.get(task.id)
    assert task.state == TaskState.DEFERRED

    # A release clears the window and returns the task to the eligible set.
    h.tasks.transition(task.id, TaskState.READY)
    task = h.tasks.get(task.id)
    assert task.state == TaskState.READY
    assert task.defer_until is None

    # Resuming stopped work counts against the retry budget.
    h.tasks.transition(task.id, TaskState.BLOCKED, reason=BlockReason.INTERRUPTED)
    resumed = h.tasks.resume(task.id, reason="test")
    assert resumed.state == TaskState.READY
    assert h.tasks.get(task.id).resume_count == 1


def test_terminal_and_invalid_transitions_rejected(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="guard rails"))
    h.tasks.transition(task.id, TaskState.CANCELLED)
    for state in (TaskState.READY, TaskState.RUNNING, TaskState.COMPLETED, TaskState.DEFERRED):
        try:
            h.tasks.transition(task.id, state)
        except ValueError:
            pass
        else:
            raise AssertionError(f"cancelled task must not become {state}")
    # Blocked self-transition is allowed for reason-only changes.
    other = h.tasks.create(TaskSpec(goal="reason change"))
    h.tasks.transition(other.id, TaskState.BLOCKED, reason=BlockReason.AWAITING_USER)
    updated = h.tasks.transition(other.id, TaskState.BLOCKED, reason=BlockReason.UNKNOWN_OUTCOME)
    assert updated.block_reason == BlockReason.UNKNOWN_OUTCOME


def test_priority_orders_eligible_work(make_harness) -> None:
    h = make_harness([])
    low = h.tasks.create(TaskSpec(goal="low priority"))
    high = h.tasks.create(TaskSpec(goal="high priority", priority=5))
    h.tasks.create(TaskSpec(goal="default priority"))
    assert h.tasks.next_ready().id == high.id
    h.tasks.set_priority(high.id, -1)
    assert h.tasks.next_ready().id == low.id


def test_deadline_is_persisted_and_notified_once(make_harness) -> None:
    h = make_harness([], settings={"scheduler": {"max_tasks_per_tick": 0}})
    task = h.tasks.create(TaskSpec(goal="due thing"))
    h.tasks.set_due(task.id, past_ts())
    first = h.scheduler.tick()
    assert any(item.kind == "due" and item.task_id == task.id for item in first.items)
    due_events = [e for e in h.events.query(limit=500) if e.operation == "task.due"]
    assert len(due_events) == 1
    # The second tick must not repeat the reminder for the same deadline.
    second = h.scheduler.tick()
    assert not any(item.kind == "due" and item.task_id == task.id for item in second.items)
    assert len([e for e in h.events.query(limit=500) if e.operation == "task.due"]) == 1


def test_checkpoints_are_persisted_and_bounded(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="progress"))
    for index in range(25):
        h.tasks.record_checkpoint(task.id, f"step {index}", {"index": index})
    task = h.tasks.get(task.id)
    assert len(task.progress) == 20
    assert task.progress[0]["summary"] == "step 5"
    assert task.progress[-1]["data"]["index"] == 24


# ------------------------------------------------------- plans vs execution


def test_plan_spans_multiple_turns_and_releases_dependency(make_harness) -> None:
    h = make_harness(
        [
            {
                "tasks": [
                    {"goal": "summarize the report"},
                    {"goal": "check the nas", "depends_on": [0]},
                ]
            },
            ask("Which report should I summarize?", "report"),
        ]
    )
    first = h.loop.handle_turn("Summarize the report, then check the nas.")
    assert len(first.tasks) == 2
    by_goal = {item.goal: item for item in first.tasks}
    summarize = by_goal["summarize the report"]
    check = by_goal["check the nas"]
    assert summarize.state == TaskState.BLOCKED
    assert summarize.block_reason == BlockReason.AWAITING_USER
    assert check.state == TaskState.BLOCKED
    assert check.block_reason == BlockReason.DEPENDENCY
    assert check.depends_on == [summarize.id]
    # The dependent has not been touched: a plan is not execution.
    assert h.capabilities.last_attempts(check.id) == []

    h.model.push(respond("Here is the quarterly summary."))
    h.model.push(act("mock", "status", target="nas"))
    h.model.push(respond("NAS is online."))
    second = h.loop.handle_turn("The quarterly report.")
    summarize = h.tasks.get(summarize.id)
    check = h.tasks.get(check.id)
    assert summarize.state == TaskState.COMPLETED
    assert check.state == TaskState.COMPLETED
    assert [a.status for a in h.capabilities.last_attempts(check.id)] == [ExecutionStatus.SUCCEEDED]


def test_proposed_plan_is_not_execution_until_approved(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="maintenance plan"))
    decision = h.control.propose_plan(
        [{"goal": "echo", "capability": "mock", "operation": "echo", "arguments": {"text": "step"}}],
        task_id=task.id,
        prompt="Run the maintenance plan?",
    )
    assert decision.status == DecisionStatus.PENDING
    assert h.tasks.get(task.id).state == TaskState.BLOCKED
    assert h.tasks.get(task.id).block_reason == BlockReason.AWAITING_CONFIRMATION
    assert h.capabilities.last_attempts(task.id) == []

    result = h.loop.handle_turn("yes")
    attempts = h.capabilities.last_attempts(task.id)
    assert [a.status for a in attempts] == [ExecutionStatus.SUCCEEDED]
    assert h.tasks.get(task.id).state == TaskState.COMPLETED
    assert h.decisions.get(decision.id).status == DecisionStatus.COMPLETED
    assert result.messages


# ------------------------------------------------------------- persistence


def test_task_survives_process_restart_and_runs(tmp_path) -> None:
    first = build_harness(tmp_path, model=ScriptedAdapter([]))
    task = first.tasks.create(TaskSpec(goal="restart survivor", priority=1))

    second = build_harness(
        tmp_path,
        model=ScriptedAdapter([act("mock", "status", target="nas"), respond("NAS is online.")]),
    )
    survived = second.tasks.get(task.id)
    assert survived.goal == "restart survivor"
    assert survived.state == TaskState.READY

    report = second.scheduler.tick()
    assert report.ran == 1
    final = second.tasks.get(task.id)
    assert final.state == TaskState.COMPLETED
    assert final.succeeded_execution
    assert final.delivered_response


def test_interrupted_task_is_recovered_after_restart(tmp_path) -> None:
    write_settings(tmp_path, COOLDOWN_ZERO)
    first = build_harness(tmp_path, model=ScriptedAdapter([]))
    task = first.tasks.create(TaskSpec(goal="interrupted work"))
    first.tasks.transition(task.id, TaskState.RUNNING)

    # Restart: the running task is marked interrupted, not silently runnable.
    second = build_harness(
        tmp_path,
        model=ScriptedAdapter([respond("Picked it back up.")]),
    )
    blocked = second.tasks.get(task.id)
    assert blocked.state == TaskState.BLOCKED
    assert blocked.block_reason == BlockReason.INTERRUPTED

    # The startup reconcile never blindly requeues (cooldown); the tick does,
    # records the resume against the retry budget, and then runs the task
    # through the normal loop.
    report = second.scheduler.tick()
    recovered = second.tasks.get(task.id)
    assert recovered.state == TaskState.COMPLETED
    assert recovered.resume_count == 1
    assert report.ran == 1
    assert any(item.kind == "requeue" and item.action == "resumed" and item.task_id == task.id for item in report.items)
    assert any("Background run finished" in str(c["summary"]) for c in recovered.progress)


def test_unknown_outcome_is_reconciled_not_repeated(tmp_path) -> None:
    write_settings(tmp_path, SCAN_ONLY)
    first = build_harness(tmp_path, model=ScriptedAdapter([]))
    task = first.tasks.create(TaskSpec(goal="uncertain write"))
    first.tasks.transition(task.id, TaskState.RUNNING)
    # Simulate a crash mid-dispatch: the pre-dispatch attempt row exists but
    # was never finalized (the executor's in-flight marker).
    from harness.contracts.actions import ExecutionResult

    attempt = ExecutionResult(
        operation_id="op_crashdead01",
        task_id=task.id,
        capability="mock",
        operation="write_note",
        status="unknown",
        data={},
        message="in flight",
        evidence={"started_at": now_ts(), "ended_at": "", "confirmation": "in_flight", "sequence": 1},
    )
    attempts_dir = first.capabilities._attempts_dir  # noqa: SLF001 - simulating a crash artifact
    atomic_write_json(
        attempts_dir / "op_crashdead01.json", asdict(attempt), retries=8, retry_s=0.05
    )

    second = build_harness(tmp_path, model=ScriptedAdapter([]))
    blocked = second.tasks.get(task.id)
    assert blocked.state == TaskState.BLOCKED
    assert blocked.block_reason == BlockReason.UNKNOWN_OUTCOME
    decisions = second.control.pending_decisions()
    assert len(decisions) == 1
    assert decisions[0].kind == "reconciliation"
    # Nothing was re-executed while the outcome was unknown.
    assert len(second.capabilities.last_attempts(task.id)) == 1

    # A tick must not requeue work with an open reconciliation decision.
    report = second.scheduler.tick()
    assert second.tasks.get(task.id).state == TaskState.BLOCKED
    assert second.tasks.get(task.id).block_reason == BlockReason.UNKNOWN_OUTCOME
    assert len(second.capabilities.last_attempts(task.id)) == 1
    assert not any(item.kind == "requeue" and item.task_id == task.id for item in report.items)

    # The user confirms completion: recorded, no re-execution.
    confirmed = build_harness(tmp_path, model=ScriptedAdapter([]))
    result = confirmed.loop.handle_turn("yes")
    final = confirmed.tasks.get(task.id)
    assert final.state == TaskState.COMPLETED
    assert final.succeeded_execution
    assert len(confirmed.capabilities.last_attempts(task.id)) == 1
    assert confirmed.decisions.get(decisions[0].id).status == DecisionStatus.COMPLETED
    assert any("confirmed" in message.lower() for message in result.messages)


def test_unknown_outcome_rejection_requeues_bounded(tmp_path) -> None:
    write_settings(tmp_path, COOLDOWN_ZERO)
    first = build_harness(tmp_path, model=ScriptedAdapter([]))
    task = first.tasks.create(TaskSpec(goal="uncertain write two"))
    first.tasks.transition(task.id, TaskState.RUNNING)
    from harness.contracts.actions import ExecutionResult

    attempt = ExecutionResult(
        operation_id="op_crashdead02",
        task_id=task.id,
        capability="mock",
        operation="write_note",
        status="unknown",
        data={},
        message="in flight",
        evidence={"started_at": now_ts(), "ended_at": "", "confirmation": "in_flight", "sequence": 1},
    )
    atomic_write_json(
        first.capabilities._attempts_dir / "op_crashdead02.json",  # noqa: SLF001
        asdict(attempt),
        retries=8,
        retry_s=0.05,
    )

    second = build_harness(tmp_path, model=ScriptedAdapter([]))
    assert second.tasks.get(task.id).block_reason == BlockReason.UNKNOWN_OUTCOME
    second.loop.handle_turn("no")
    rejected_task = second.tasks.get(task.id)
    assert rejected_task.state == TaskState.READY
    assert rejected_task.resume_count == 1
    decision = second.control.pending_decisions()
    assert decision == []
    stored = second.decisions.list(status="rejected")
    assert stored and stored[0].kind == "reconciliation"


# ------------------------------------------------------------ dependencies


def test_scheduler_releases_stale_dependency_blocks(make_harness) -> None:
    h = make_harness([], settings=SCAN_ONLY)
    dep = h.tasks.create(TaskSpec(goal="prerequisite"))
    dependent = h.tasks.create(TaskSpec(goal="follow up", depends_on=[dep.id]))
    assert dependent.state == TaskState.BLOCKED

    h.tasks.transition(dep.id, TaskState.RUNNING)
    h.tasks.transition(dep.id, TaskState.COMPLETED)
    # Normal release already happened; simulate a stale row by re-blocking.
    h.tasks.transition(dependent.id, TaskState.BLOCKED, reason=BlockReason.DEPENDENCY)

    report = h.scheduler.tick()
    assert any(
        item.kind == "dependency" and item.action == "released" and item.task_id == dependent.id
        for item in report.items
    )
    assert h.tasks.get(dependent.id).state == TaskState.READY


def test_failed_dependency_fails_dependent(make_harness) -> None:
    h = make_harness([], settings=SCAN_ONLY)
    dep = h.tasks.create(TaskSpec(goal="doomed prerequisite"))
    dependent = h.tasks.create(TaskSpec(goal="dependent work", depends_on=[dep.id]))
    h.tasks.transition(dep.id, TaskState.RUNNING)
    h.tasks.transition(dep.id, TaskState.FAILED)

    report = h.scheduler.tick()
    final = h.tasks.get(dependent.id)
    assert final.state == TaskState.FAILED
    assert final.block_reason == "dependency_failed"
    assert any(item.kind == "dependency" and item.action == "failed" for item in report.items)
    # A failed dependent is never requeued by the retry path: its own attempts
    # show nothing retryable.
    assert not any(item.kind == "requeue" and item.task_id == dependent.id for item in report.items)


# ---------------------------------------------------------------- approval


def test_task_waiting_for_approval_is_not_run_by_scheduler(make_harness) -> None:
    h = make_harness(
        [
            plan("write note"),
            {
                "kind": "act",
                "capability": "mock",
                "operation": "write_note",
                "arguments": {"text": "draft"},
                "requires_confirmation": True,
            },
        ],
        settings={"capabilities": {"policy": {"write": "allow"}}},
    )
    first = h.loop.handle_turn("Write a note.")
    task = first.tasks[0]
    assert task.state == TaskState.BLOCKED
    assert task.block_reason == BlockReason.AWAITING_CONFIRMATION
    assert h.capabilities.last_attempts(task.id) == []

    h.scheduler.tick()
    still = h.tasks.get(task.id)
    assert still.state == TaskState.BLOCKED
    assert still.block_reason == BlockReason.AWAITING_CONFIRMATION
    assert h.capabilities.last_attempts(task.id) == []

    work = h.scheduler.report()
    assert any(item.id == task.id for item in work.needs_approval)
    assert work.open_decisions and work.open_decisions[0].kind == "action"


def test_deferred_decision_holds_work_until_answered(make_harness) -> None:
    h = make_harness([], settings={"scheduler": {"default_defer_hours": 1.0}})
    task = h.tasks.create(TaskSpec(goal="deferred proposal"))
    decision = h.control.propose_action(task.id, "mock", "echo", {"text": "later"})

    h.loop.handle_turn("not yet")
    deferred = h.tasks.get(task.id)
    assert deferred.state == TaskState.DEFERRED
    assert deferred.defer_until
    assert h.decisions.get(decision.id).status == DecisionStatus.DEFERRED

    # Even with the window long past, an open decision still holds the work.
    h.tasks.defer(task.id, past_ts(), reason="window passed")
    h.scheduler.tick()
    assert h.tasks.get(task.id).state == TaskState.DEFERRED

    # The user's approval (not the clock) resumes the deferred work.
    h.loop.handle_turn("yes")
    final = h.tasks.get(task.id)
    assert final.state == TaskState.COMPLETED
    assert [a.status for a in h.capabilities.last_attempts(task.id)] == [ExecutionStatus.SUCCEEDED]


# --------------------------------------------------------------- deferrals


def test_deferred_task_becomes_eligible_and_runs(make_harness) -> None:
    h = make_harness(
        [act("mock", "status", target="nas"), respond("NAS is online.")],
        settings=COOLDOWN_ZERO,
    )
    task = h.tasks.create(TaskSpec(goal="postponed check"))
    h.tasks.defer(task.id, future_ts(3600), reason="user said tomorrow")
    first = h.scheduler.tick()
    assert h.tasks.get(task.id).state == TaskState.DEFERRED
    assert not any(item.task_id == task.id for item in first.items)

    h.tasks.defer(task.id, past_ts(), reason="window reached")
    second = h.scheduler.tick()
    assert any(
        item.kind == "deferred" and item.action == "released" and item.task_id == task.id
        for item in second.items
    )
    assert second.ran == 1
    final = h.tasks.get(task.id)
    assert final.state == TaskState.COMPLETED
    assert final.succeeded_execution
    assert any("Background run finished" in str(c["summary"]) for c in final.progress)


# ------------------------------------------------------------ retry limits


def test_failed_task_reaches_bounded_retry_limit(make_harness) -> None:
    h = make_harness(
        [],
        settings={
            "scheduler": {
                "max_task_retries": 1,
                "retry_cooldown_s": 0.0,
                "max_tasks_per_tick": 0,
            }
        },
    )
    task = h.tasks.create(TaskSpec(goal="flaky operation"))

    def fail_once() -> None:
        result = h.capabilities.execute(
            ExecuteRequest(
                task_id=task.id,
                domain_id=None,
                capability="mock",
                operation="fail",
                arguments={"category": "transient"},
            )
        )
        assert result.status == ExecutionStatus.FAILED
        h.tasks.transition(task.id, TaskState.RUNNING)
        h.tasks.transition(task.id, TaskState.FAILED)

    fail_once()
    first = h.scheduler.tick()
    # Retryable failure: requeued once (budget 1).
    assert h.tasks.get(task.id).state == TaskState.READY
    assert h.tasks.get(task.id).resume_count == 1
    assert any(item.kind == "requeue" and item.action == "resumed" for item in first.items)

    fail_once()
    second = h.scheduler.tick()
    final = h.tasks.get(task.id)
    assert final.state == TaskState.FAILED
    assert any(
        item.kind == "requeue" and item.action == "retry_limit" for item in second.items
    )
    assert any((c.get("data") or {}).get("kind") == "retry_limit" for c in final.progress)
    exhausted = [e for e in h.events.query(limit=500) if e.operation == "task.retry_exhausted"]
    assert len(exhausted) == 1

    # Further ticks stay quiet: no endless retry loop.
    third = h.scheduler.tick()
    assert h.tasks.get(task.id).state == TaskState.FAILED
    assert len([e for e in h.events.query(limit=500) if e.operation == "task.retry_exhausted"]) == 1


def test_non_retryable_failure_is_never_requeued(make_harness) -> None:
    h = make_harness([], settings=COOLDOWN_ZERO)
    task = h.tasks.create(TaskSpec(goal="hard failure"))
    result = h.capabilities.execute(
        ExecuteRequest(
            task_id=task.id,
            domain_id=None,
            capability="mock",
            operation="fail",
            arguments={"category": "hard"},
        )
    )
    assert result.status == ExecutionStatus.FAILED
    h.tasks.transition(task.id, TaskState.RUNNING)
    h.tasks.transition(task.id, TaskState.FAILED)

    h.scheduler.tick()
    assert h.tasks.get(task.id).state == TaskState.FAILED
    assert h.tasks.get(task.id).resume_count == 0


# ------------------------------------------------------------ cancellation


def test_cancellation_prevents_execution(make_harness) -> None:
    h = make_harness(
        [act("mock", "status", target="nas"), respond("NAS is online.")],
        settings=COOLDOWN_ZERO,
    )
    keep = h.tasks.create(TaskSpec(goal="keep this running"))
    gone = h.tasks.create(TaskSpec(goal="remove the scratch notes"))
    h.tasks.set_due(gone.id, past_ts())

    result = h.loop.handle_turn("cancel the scratch notes")
    assert h.tasks.get(gone.id).state == TaskState.CANCELLED

    report = h.scheduler.tick()
    assert report.ran == 1
    assert h.tasks.get(keep.id).state == TaskState.COMPLETED
    # Cancelled work is never resurrected, run, or reminded.
    assert h.tasks.get(gone.id).state == TaskState.CANCELLED
    assert h.capabilities.last_attempts(gone.id) == []
    assert not any(item.task_id == gone.id for item in report.items)
    assert not [e for e in h.events.query(limit=500) if e.operation == "task.due" and e.target == gone.id]
    work = h.scheduler.report()
    assert not any(item.id == gone.id for item in work.ready + work.deferred + work.due)


# --------------------------------------------------------- deterministicism


def test_scheduled_event_processed_without_model_polling(make_harness) -> None:
    h = make_harness(
        [],
        settings={"scheduler": {"max_tasks_per_tick": 0, "retry_cooldown_s": 0.0}},
    )
    due_task = h.tasks.create(TaskSpec(goal="due thing"))
    h.tasks.set_due(due_task.id, past_ts())
    deferred_task = h.tasks.create(TaskSpec(goal="postponed thing"))
    h.tasks.defer(deferred_task.id, past_ts(), reason="window reached")

    report = h.scheduler.tick()
    assert report.model_used is False
    assert report.ran == 0
    # Both deterministic findings happened with zero model calls.
    assert h.tasks.get(deferred_task.id).state == TaskState.READY
    assert any(item.kind == "due" and item.task_id == due_task.id for item in report.items)
    model_calls = [e for e in h.events.query(limit=1000) if e.operation == "model.call"]
    assert model_calls == []


def test_disabled_scheduler_heartbeat_is_inert(make_harness) -> None:
    h = make_harness([], settings={"scheduler": {"enabled": False}})
    task = h.tasks.create(TaskSpec(goal="paused work"))
    h.tasks.defer(task.id, past_ts(), reason="window reached")
    report = h.scheduler.tick()
    assert report.enabled is False
    assert report.items == []
    assert h.tasks.get(task.id).state == TaskState.DEFERRED
    # The honest-status read model still works with the heartbeat off.
    work = h.scheduler.report()
    assert any(item.id == task.id for item in work.deferred)


def test_tick_is_quiesced_behind_the_exclusive_gate(make_harness) -> None:
    h = make_harness([])
    box: dict[str, object] = {}
    started = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with h.scheduler.exclusive("reset"):
            started.set()
            release.wait(timeout=10)

    def run_tick() -> None:
        box["report"] = h.scheduler.tick()

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert started.wait(timeout=5)
    ticker = threading.Thread(target=run_tick, daemon=True)
    ticker.start()
    # While the reset holds the gate, the heartbeat waits instead of racing it.
    time.sleep(0.2)
    assert ticker.is_alive()
    release.set()
    holder.join(timeout=5)
    ticker.join(timeout=5)
    assert not ticker.is_alive()
    # After the gate opens, ticks proceed normally.
    assert h.scheduler.tick().enabled is True


def test_scheduled_consolidation_runs_from_tick(make_harness) -> None:
    h = make_harness(
        [],
        settings={
            "consolidation": {
                "schedule_enabled": True,
                "extract_enabled": False,
            }
        },
    )
    first = h.scheduler.tick()
    assert any(item.kind == "consolidation" and item.action == "ran" for item in first.items)
    assert h.consolidation.list(1)

    # Within the interval the tick does not run it again.
    second = h.scheduler.tick()
    assert not any(item.kind == "consolidation" for item in second.items)


# ------------------------------------------------------------- honest status


def test_execution_evidence_distinguishes_planned_attempted_completed(make_harness) -> None:
    h = make_harness([], settings=COOLDOWN_ZERO)
    planned = h.tasks.create(TaskSpec(goal="planned but not started"))

    attempted = h.tasks.create(TaskSpec(goal="attempted and failed"))
    h.capabilities.execute(
        ExecuteRequest(
            task_id=attempted.id,
            domain_id=None,
            capability="mock",
            operation="fail",
            arguments={"category": "hard"},
        )
    )
    h.tasks.transition(attempted.id, TaskState.RUNNING)
    h.tasks.transition(attempted.id, TaskState.FAILED)

    done = h.tasks.create(TaskSpec(goal="actually done"))
    h.capabilities.execute(
        ExecuteRequest(
            task_id=done.id,
            domain_id=None,
            capability="mock",
            operation="status",
            arguments={"target": "nas"},
        )
    )
    h.tasks.transition(done.id, TaskState.RUNNING)
    h.tasks.mark_succeeded_execution(done.id)
    h.tasks.mark_delivered(done.id, "NAS is online.")
    h.tasks.transition(done.id, TaskState.COMPLETED)
    from harness.contracts.tasks import TaskOutcome

    h.tasks.record_outcome(done.id, TaskOutcome(summary="NAS is online.", status="completed"))

    work = h.scheduler.report()
    ready_ids = [item.id for item in work.ready]
    assert planned.id in ready_ids
    planned_item = next(item for item in work.ready if item.id == planned.id)
    assert planned_item.attempts == 0
    assert planned_item.succeeded_execution is False
    assert planned_item.delivered_response is False

    failed_item = next(item for item in work.failed if item.id == attempted.id)
    assert failed_item.attempts == 1
    assert failed_item.last_attempt_status == "failed"
    assert failed_item.last_attempt_operation == "mock.fail"
    assert failed_item.succeeded_execution is False

    completed_item = next(item for item in work.completed if item.id == done.id)
    assert completed_item.attempts == 1
    assert completed_item.last_attempt_status == "succeeded"
    assert completed_item.succeeded_execution is True
    assert completed_item.delivered_response is True

    assert planned.id in work.summary()


def test_report_answers_waiting_on_you(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="waiting for input"))
    h.tasks.transition(task.id, TaskState.BLOCKED, reason=BlockReason.AWAITING_USER)

    work = h.scheduler.report()
    waiting = next(item for item in work.waiting if item.id == task.id)
    assert waiting.waiting_on == "user"
    assert work.next_hint.startswith("wait for your answer")

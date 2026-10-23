from __future__ import annotations

from harness.constants import ActionKind, BlockReason, ExecutionStatus, TaskState
from harness.contracts.actions import (
    ActAction,
    AskAction,
    ContinueAction,
    EndAction,
    ExecutionResult,
    RespondAction,
)
from harness.contracts.tasks import Task
from harness.orchestrator.machine import allowed_actions, apply_action


def task(**overrides) -> Task:
    values = {
        "id": "tsk_1",
        "goal": "test",
        "state": TaskState.RUNNING,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    return Task(**values)


def test_all_five_actions_are_allowed_while_running() -> None:
    assert allowed_actions(TaskState.RUNNING) == set(ActionKind)


def test_actions_are_rejected_in_non_running_states() -> None:
    for state in TaskState:
        if state == TaskState.RUNNING:
            continue
        result = apply_action(task(state=state), RespondAction(message="x"))
        assert result.error


def test_respond_requires_message_and_completes() -> None:
    assert apply_action(task(), RespondAction(message=" ")).error
    result = apply_action(task(), RespondAction(message="done"))
    assert result.task_state == TaskState.COMPLETED
    assert result.complete and result.deliver == "done"


def test_ask_blocks_awaiting_user() -> None:
    assert apply_action(task(), AskAction(question="Which one?", missing="target")).error is None
    result = apply_action(task(), AskAction(question="Which one?", missing="target"))
    assert result.task_state == TaskState.BLOCKED
    assert result.block_reason == BlockReason.AWAITING_USER
    assert result.deliver == "Which one?"


def test_continue_stays_running() -> None:
    result = apply_action(task(), ContinueAction(purpose="next"))
    assert result.task_state == TaskState.RUNNING
    assert result.error is None


def test_end_completed_requires_evidence() -> None:
    error = apply_action(task(), EndAction(status="completed", summary="done"))
    assert error.error
    ok = apply_action(task(delivered_response=True), EndAction(status="completed", summary="done"))
    assert ok.complete and ok.task_state == TaskState.COMPLETED
    ok = apply_action(task(succeeded_execution=True), EndAction(status="completed", summary="done"))
    assert ok.complete


def test_end_cancelled_and_abandoned() -> None:
    cancelled = apply_action(task(), EndAction(status="cancelled", summary="no"))
    assert cancelled.task_state == TaskState.CANCELLED
    abandoned = apply_action(task(), EndAction(status="abandoned", summary="stop"))
    assert abandoned.task_state == TaskState.CANCELLED
    assert abandoned.block_reason == "abandoned"


def test_act_requires_capability_and_operation() -> None:
    assert apply_action(task(), ActAction(capability="", operation="x")).error
    assert apply_action(task(), ActAction(capability="mock", operation="")).error
    result = apply_action(task(), ActAction(capability="mock", operation="ping"))
    assert result.execute is not None
    result = apply_action(
        task(),
        ActAction(capability="mock", operation="ping"),
        ExecutionResult(
            operation_id="op_1",
            task_id="tsk_1",
            capability="mock",
            operation="ping",
            status=ExecutionStatus.SUCCEEDED,
            data={},
            message="ok",
        ),
    )
    assert result.error is None and result.task_state == TaskState.RUNNING

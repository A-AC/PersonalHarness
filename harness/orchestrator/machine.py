from __future__ import annotations

from dataclasses import dataclass

from harness.constants import ActionKind, BlockReason, EndStatus, ExecutionStatus, TaskState
from harness.contracts.actions import ActAction, AskAction, ContinueAction, EndAction, ExecutionResult, Proposal, RespondAction
from harness.contracts.tasks import Task


@dataclass
class MachineResult:
    task_state: str
    block_reason: str | None
    deliver: str | None
    execute: ActAction | None
    error: str | None = None
    complete: bool = False


ALLOWED = {
    TaskState.RUNNING: {
        ActionKind.RESPOND,
        ActionKind.ASK,
        ActionKind.ACT,
        ActionKind.CONTINUE,
        ActionKind.END,
    }
}


def allowed_actions(state: str) -> set[str]:
    return set(ALLOWED.get(state, set()))


def apply_action(task: Task, proposal: Proposal, execution: ExecutionResult | None = None) -> MachineResult:
    if task.state != TaskState.RUNNING:
        return MachineResult(task.state, task.block_reason, None, None, error=f"action not allowed in state {task.state}")
    kind = proposal.kind
    if kind not in allowed_actions(task.state):
        return MachineResult(task.state, task.block_reason, None, None, error=f"unknown action {kind}")
    if isinstance(proposal, RespondAction):
        if not proposal.message.strip():
            return MachineResult(task.state, None, None, None, error="respond.message required")
        return MachineResult(TaskState.COMPLETED, None, proposal.message, None, complete=True)
    if isinstance(proposal, AskAction):
        if not proposal.question.strip() or not proposal.missing.strip():
            return MachineResult(task.state, None, None, None, error="ask requires question and missing")
        return MachineResult(TaskState.BLOCKED, BlockReason.AWAITING_USER, proposal.question, None)
    if isinstance(proposal, ContinueAction):
        if not proposal.purpose.strip():
            return MachineResult(task.state, None, None, None, error="continue.purpose required")
        return MachineResult(TaskState.RUNNING, None, None, None)
    if isinstance(proposal, EndAction):
        if proposal.status == EndStatus.COMPLETED:
            if not (task.delivered_response or task.succeeded_execution):
                return MachineResult(
                    task.state,
                    None,
                    None,
                    None,
                    error="end.completed requires a delivered response or succeeded execution",
                )
            return MachineResult(TaskState.COMPLETED, None, proposal.summary or None, None, complete=True)
        if proposal.status == EndStatus.CANCELLED:
            return MachineResult(TaskState.CANCELLED, None, proposal.summary or None, None, complete=True)
        if proposal.status == EndStatus.ABANDONED:
            return MachineResult(TaskState.CANCELLED, "abandoned", proposal.summary or None, None, complete=True)
        return MachineResult(task.state, None, None, None, error="invalid end status")
    if isinstance(proposal, ActAction):
        if not proposal.capability or not proposal.operation:
            return MachineResult(task.state, None, None, None, error="act requires capability and operation")
        if execution is None:
            return MachineResult(TaskState.RUNNING, None, None, proposal)
        if execution.status == ExecutionStatus.SUCCEEDED:
            return MachineResult(TaskState.RUNNING, None, None, None)
        if execution.status == ExecutionStatus.UNAVAILABLE:
            return MachineResult(TaskState.RUNNING, None, None, None)
        return MachineResult(TaskState.RUNNING, None, None, None)
    return MachineResult(task.state, None, None, None, error="unhandled action")

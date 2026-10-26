from __future__ import annotations

from pathlib import Path

from harness.app import build_harness
from harness.constants import DecisionStatus, ExecutionStatus, TaskState
from harness.contracts.domains import StructureDraft
from harness.contracts.tasks import TaskSpec
from harness.model.scripted import ScriptedAdapter
from tests.conftest import act, plan, respond


def confirmation_act(capability: str, operation: str, **arguments) -> dict:
    return {
        "kind": "act",
        "capability": capability,
        "operation": operation,
        "arguments": arguments,
        "requires_confirmation": True,
    }


def draft(name: str) -> StructureDraft:
    return StructureDraft(name=name, reason="test", evidence="test")


def test_single_yes_approves_and_executes_exactly_once(make_harness) -> None:
    h = make_harness(
        [
            plan("write note"),
            confirmation_act("mock", "write_note", text="approved"),
        ],
        settings={"capabilities": {"policy": {"write": "allow"}}},
    )

    first = h.loop.handle_turn("Write a note.")
    assert "confirmation" in first.messages[0].lower()
    assert h.capabilities.last_attempts(first.tasks[0].id) == []
    pending = h.control.pending_decisions()
    assert len(pending) == 1
    assert pending[0].status == DecisionStatus.PENDING

    second = h.loop.handle_turn("yes")
    attempts = h.capabilities.last_attempts(second.tasks[0].id)
    assert [item.status for item in attempts] == [ExecutionStatus.SUCCEEDED]
    decision = h.decisions.get(pending[0].id)
    assert decision.status == DecisionStatus.COMPLETED
    assert decision.consumed_at
    assert any(event.operation == "decision.execution" for event in h.events.query())


def test_single_no_rejects_without_executing(make_harness) -> None:
    h = make_harness(
        [
            plan("write note"),
            confirmation_act("mock", "write_note", text="rejected"),
        ],
        settings={"capabilities": {"policy": {"write": "allow"}}},
    )

    first = h.loop.handle_turn("Write a note.")
    pending = h.control.pending_decisions()[0]
    second = h.loop.handle_turn("no")

    assert "won't run" in second.messages[0].lower()
    assert h.capabilities.last_attempts(first.tasks[0].id) == []
    decision = h.decisions.get(pending.id)
    assert decision.status == DecisionStatus.REJECTED
    assert h.tasks.get(first.tasks[0].id).state == TaskState.CANCELLED


def test_yes_but_do_not_restart_removes_only_that_step(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="maintenance plan"))
    decision = h.control.propose_plan(
        [
            {
                "goal": "Restart the service",
                "capability": "mock",
                "operation": "echo",
                "arguments": {"text": "restart"},
            },
            {
                "goal": "Check the status",
                "capability": "mock",
                "operation": "status",
                "arguments": {"target": "nas"},
            },
        ],
        task_id=task.id,
        prompt="Run the maintenance plan?",
    )

    result = h.loop.handle_turn("yes, but don't restart")

    assert result.messages
    operations = [item.operation for item in h.capabilities.last_attempts(task.id)]
    assert operations == ["status"]
    updated = h.decisions.get(decision.id)
    assert updated.status == DecisionStatus.COMPLETED
    assert "restart" in str(updated.proposal.get("modifications"))


def test_bare_yes_with_multiple_pending_decisions_asks_which_one(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="two proposals"))
    first = h.control.propose_action(task.id, "mock", "echo", {"text": "one"})
    second = h.control.propose_action(task.id, "mock", "status", {"target": "two"})

    result = h.loop.handle_turn("yes")

    assert "more than one pending decision" in result.messages[0].lower()
    assert h.decisions.get(first.id).status == DecisionStatus.PENDING
    assert h.decisions.get(second.id).status == DecisionStatus.PENDING
    assert h.capabilities.last_attempts(task.id) == []


def test_ambiguous_yes_can_be_resolved_by_an_ordinal(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="ordinal proposals"))
    first = h.control.propose_action(task.id, "mock", "echo", {"text": "one"})
    second = h.control.propose_action(task.id, "mock", "status", {"target": "two"})

    result = h.loop.handle_turn("yes, the second one")

    assert result.messages
    assert [item.operation for item in h.capabilities.last_attempts(task.id)] == ["status"]
    assert h.decisions.get(first.id).status == DecisionStatus.PENDING
    assert h.decisions.get(second.id).status == DecisionStatus.COMPLETED


def test_unrelated_question_while_a_decision_is_pending_is_not_approval(make_harness) -> None:
    h = make_harness(
        [respond("I don't know the weather.")],
        settings={"tasks": {"planning_mode": "heuristic"}},
    )
    task = h.tasks.create(TaskSpec(goal="pending proposal"))
    decision = h.control.propose_action(task.id, "mock", "echo", {"text": "hold"})

    result = h.loop.handle_turn("What's the weather?")

    assert result.messages == ["I don't know the weather."]
    assert h.decisions.get(decision.id).status == DecisionStatus.PENDING
    assert h.capabilities.last_attempts(task.id) == []


def test_question_is_not_approval(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="explain proposal"))
    decision = h.control.propose_action(task.id, "mock", "echo", {"text": "hello"})

    result = h.loop.handle_turn("What exactly will that do?")

    assert "haven't acted yet" in result.messages[0].lower()
    assert h.decisions.get(decision.id).status == DecisionStatus.PENDING
    assert h.capabilities.last_attempts(task.id) == []


def test_defer_then_yes_approves_the_same_decision(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="defer proposal"))
    decision = h.control.propose_action(task.id, "mock", "echo", {"text": "later"})

    deferred = h.loop.handle_turn("not yet")
    assert "deferred" in deferred.messages[0].lower()
    assert h.decisions.get(decision.id).status == DecisionStatus.DEFERRED
    assert h.capabilities.last_attempts(task.id) == []

    h.loop.handle_turn("yes")
    assert [item.status for item in h.capabilities.last_attempts(task.id)] == [
        ExecutionStatus.SUCCEEDED
    ]
    assert h.decisions.get(decision.id).status == DecisionStatus.COMPLETED


def test_cancel_one_task_leaves_the_other_active(make_harness) -> None:
    h = make_harness([])
    garden = h.tasks.create(TaskSpec(goal="water the garden"))
    garage = h.tasks.create(TaskSpec(goal="clean the garage"))

    result = h.loop.handle_turn("cancel the garden task")

    assert "garden" in result.messages[0].lower()
    assert h.tasks.get(garden.id).state == TaskState.CANCELLED
    assert h.tasks.get(garage.id).state == TaskState.READY


def test_unrelated_request_while_a_decision_is_pending_is_planned(make_harness) -> None:
    h = make_harness(
        [respond("New work done.")],
        settings={"tasks": {"planning_mode": "heuristic"}},
    )
    pending_task = h.tasks.create(TaskSpec(goal="pending proposal"))
    decision = h.control.propose_action(pending_task.id, "mock", "echo", {"text": "hold"})

    result = h.loop.handle_turn("check the nas")

    assert result.messages == ["New work done."]
    assert h.decisions.get(decision.id).status == DecisionStatus.PENDING
    assert h.capabilities.last_attempts(pending_task.id) == []
    assert len(result.tasks) == 2


def test_pending_decision_survives_restart_and_can_be_approved(tmp_path: Path) -> None:
    first = build_harness(tmp_path, model=ScriptedAdapter([]))
    task = first.tasks.create(TaskSpec(goal="restart-sensitive work"))
    decision = first.control.propose_action(task.id, "mock", "echo", {"text": "persisted"})

    restarted = build_harness(tmp_path, model=ScriptedAdapter([]))
    recovered = restarted.control.pending_decisions()
    assert [item.id for item in recovered] == [decision.id]
    assert recovered[0].prompt

    restarted.loop.handle_turn("yes")
    assert [item.status for item in restarted.capabilities.last_attempts(task.id)] == [
        ExecutionStatus.SUCCEEDED
    ]
    assert restarted.decisions.get(decision.id).status == DecisionStatus.COMPLETED


def test_approval_does_not_bypass_capability_permissions(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="delete protected note"))
    decision = h.control.propose_action(
        task.id, "mock", "delete_note", {"name": "protected"}
    )

    result = h.loop.handle_turn("yes")

    attempts = h.capabilities.last_attempts(task.id)
    assert [item.status for item in attempts] == [ExecutionStatus.DENIED]
    assert "capabilities.policy.destructive" in attempts[0].message
    assert "couldn't complete" in result.messages[0].lower()
    assert h.decisions.get(decision.id).status == DecisionStatus.FAILED


def test_rejected_proposal_never_executes(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="reject proposal"))
    decision = h.control.propose_action(task.id, "mock", "echo", {"text": "never"})

    h.loop.handle_turn("no")

    assert h.capabilities.last_attempts(task.id) == []
    assert h.decisions.get(decision.id).status == DecisionStatus.REJECTED


def test_approved_failure_is_reported_honestly(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="approved failing action"))
    decision = h.control.propose_action(
        task.id, "mock", "fail", {"category": "hard_failure"}
    )

    result = h.loop.handle_turn("yes")

    attempts = h.capabilities.last_attempts(task.id)
    assert [item.status for item in attempts] == [ExecutionStatus.FAILED]
    assert "couldn't complete" in result.messages[0].lower()
    assert "not reported it as done" in result.messages[0].lower()
    assert h.decisions.get(decision.id).status == DecisionStatus.FAILED


def test_conversation_reset_removes_pending_decisions(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="reset decision"))
    h.control.propose_action(task.id, "mock", "echo", {"text": "gone"})

    h.reset.run("conversations", backup=False)

    assert h.control.pending_decisions() == []
    assert h.tasks.list() == []


def test_domain_suggestion_decision_tracks_registry(make_harness) -> None:
    h = make_harness([])
    suggestion = h.domains.record_suggestion(draft("homelab"))

    pending = h.control.pending_decisions()
    assert len(pending) == 1
    assert pending[0].kind == "domain_creation"

    h.loop.handle_turn("yes")
    assert h.domains.find_domain_by_name("homelab") is not None
    assert h.domains.list_suggestions()[0].status == "confirmed"
    assert h.decisions.find_for_suggestion(suggestion.id).status == DecisionStatus.COMPLETED

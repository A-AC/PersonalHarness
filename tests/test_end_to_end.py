"""End-to-end behavior scenarios for the integrated assistant.

Each test drives the public entry points (``build_harness``, the interaction
loop, the control plane, the scheduler, and the learning review) against an
isolated data directory. Scripted model output is used only where a real model
call would otherwise be required; the persisted managers, permissions,
decision lifecycle, retrieval, recovery, and skill disclosure are real.

History summarisation is disabled for these scenarios so a background summary
call cannot consume a scripted plan/proposal. That is a test-harness concern,
not a change to production defaults.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harness.app import build_harness
from harness.constants import (
    BlockReason,
    DecisionStatus,
    ExecutionStatus,
    TaskState,
)
from harness.contracts.domains import StructureDraft
from harness.contracts.learning import SkillDraft
from harness.contracts.memory import MemoryCandidate
from harness.contracts.tasks import TaskSpec
from harness.learning.skills import SkillValidationError
from harness.model.scripted import ScriptedAdapter
from tests.conftest import (
    act,
    ask,
    plan,
    respond,
    write_settings,
)

SETTINGS = {
    "context": {"history": {"strategy": "full", "summaries": False}},
    # These scenarios repeat the same subject on purpose to exercise learning,
    # not the separate repeated-subject domain suggestion path.
    "domain": {"suggestion": {"threshold": 50}},
}
SCAN_ONLY = {
    **SETTINGS,
    "scheduler": {"max_tasks_per_tick": 0, "retry_cooldown_s": 0.0},
}


def draft(name: str) -> StructureDraft:
    return StructureDraft(name=name, reason="end-to-end scenario", evidence="test fixture")


def make_domain(h, name: str):
    return h.domains.create_domain(draft(name), confirmed=True)


def remember(h, content: str, *, scope: str = "global", type: str | None = None):
    source = h.sessions.record_interaction(f"source for {content}")
    submitted = h.memory.submit(
        MemoryCandidate(
            content=content,
            origin="explicit",
            type_hint=type,
            scope_hint=scope,
            default_scope=scope,
            provenance={
                "source_kind": "user_statement",
                "source_refs": [source.id],
                "evidence_status": "stated",
            },
        )
    )
    assert submitted.record is not None
    return submitted.record


def confirmation_act(capability: str, operation: str, **arguments):
    return {
        "kind": "act",
        "capability": capability,
        "operation": operation,
        "arguments": arguments,
        "requires_confirmation": True,
    }


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


def prompt_text(traces) -> str:
    return "\n".join(
        str(message.get("content") or "")
        for trace in traces
        if trace.model_called
        for message in trace.messages
    )


def included_items(traces) -> set[str]:
    return {
        str(row.get("item"))
        for trace in traces
        for row in trace.included
        if row.get("item")
    }


# --------------------------------------------------------------- new projects


def test_user_starts_a_new_project_by_explicit_command(make_harness) -> None:
    h = make_harness([], settings=SETTINGS)

    result = h.loop.handle_turn("start a new domain about garden")

    garden = h.domains.find_domain_by_name("garden")
    assert garden is not None
    assert "Created domain 'garden'" in result.messages[0]
    # A second project is a separate record, not an alias of the first.
    h.loop.handle_turn("start a new domain about finances")
    names = {item.name for item in h.domains.list_domains()}
    assert {"garden", "finances"} <= names


def test_existing_project_is_inferred_without_manual_selection(make_harness) -> None:
    h = make_harness(
        [
            plan("check the garden irrigation controller"),
            respond("The garden controller is reachable."),
        ],
        settings=SETTINGS,
    )
    garden = make_domain(h, "garden")
    homelab = make_domain(h, "homelab")
    h.sessions.set_active_domain(homelab.id)
    evidence = remember(
        h,
        "The garden irrigation controller is at 192.168.1.50",
        scope=f"domain:{garden.id}",
    )

    result = h.loop.handle_turn("check the garden irrigation controller")

    task = result.tasks[-1]
    assert task.domain_id == garden.id
    # Inference routes new work; it does not silently change the user's
    # explicit session selection.
    assert h.sessions.active_domain() == homelab.id
    events = [
        item
        for item in h.events.query(limit=500)
        if item.operation == "plan.domain_inference"
    ]
    assert events and events[-1].data["resolved"] is True
    assert events[-1].data["matches"][0]["domain_id"] == garden.id
    traces = h.traces.list_for(interaction_id=result.interaction_id)
    assert evidence.id in included_items(traces)


def test_two_projects_stay_distinct_in_one_session(make_harness) -> None:
    h = make_harness(
        [
            {
                "tasks": [
                    {"goal": "check the rack switch", "domain_name": "homelab", "depends_on": []},
                    {"goal": "check the garden valve", "domain_name": "garden", "depends_on": []},
                ]
            },
            respond("Rack checked."),
            respond("Valve checked."),
        ],
        settings=SETTINGS,
    )
    homelab = make_domain(h, "homelab")
    garden = make_domain(h, "garden")
    lab_memory = remember(h, "The rack switch is at 10.0.0.2", scope=f"domain:{homelab.id}")
    garden_memory = remember(h, "The garden valve closes at dusk", scope=f"domain:{garden.id}")

    result = h.loop.handle_turn("check the rack switch and check the garden valve")

    by_goal = {item.goal: item for item in result.tasks}
    assert by_goal["check the rack switch"].domain_id == homelab.id
    assert by_goal["check the garden valve"].domain_id == garden.id
    assert result.messages == ["Rack checked.", "Valve checked."]

    lab_context = h.context.build(by_goal["check the rack switch"], h.sessions.current())
    garden_context = h.context.build(by_goal["check the garden valve"], h.sessions.current())
    assert lab_memory.id in {item.item for item in lab_context.included}
    assert garden_memory.id in {item.item for item in garden_context.included}
    # Cross-project retrieval is intentional, but task association, project
    # primary, and stored memory scope remain distinct.
    assert lab_context.stats["project"]["primary_domain_id"] == homelab.id
    assert garden_context.stats["project"]["primary_domain_id"] == garden.id
    assert h.memory.get(lab_memory.id).scope == f"domain:{homelab.id}"
    assert h.memory.get(garden_memory.id).scope == f"domain:{garden.id}"
    assert h.tasks.get(by_goal["check the rack switch"].id).domain_id == homelab.id
    assert h.tasks.get(by_goal["check the garden valve"].id).domain_id == garden.id


# ------------------------------------------------------ memory across sessions


def test_prior_session_knowledge_is_recalled_after_restart(make_harness) -> None:
    h = make_harness([], settings=SETTINGS)
    source = h.sessions.record_interaction("Earlier we named the workshop NAS timber")
    record = h.memory.submit(
        MemoryCandidate(
            content="The workshop NAS is named timber",
            default_scope="global",
            provenance={
                "source_kind": "user_statement",
                "source_refs": [source.id],
                "evidence_status": "stated",
            },
        )
    ).record
    assert record is not None

    restarted = build_harness(
        h.data_dir,
        model=ScriptedAdapter([plan("what is the workshop NAS called?"), respond("It is called timber.")]),
    )
    result = restarted.loop.handle_turn("what is the workshop NAS called?")

    assert result.messages == ["It is called timber."]
    traces = restarted.traces.list_for(interaction_id=result.interaction_id)
    assert record.id in included_items(traces)
    assert "timber" in prompt_text(traces).lower()
    # Both the memory and the original interaction survived the restart.
    assert restarted.memory.get(record.id).content == "The workshop NAS is named timber"
    assert any(
        "workshop NAS" in item.user_text
        for item in restarted.sessions.list_interactions(limit=None)
    )


def test_user_correction_supersedes_a_preference_for_later_turns(make_harness) -> None:
    h = make_harness([], settings=SETTINGS)
    old = remember(h, "The user prefers concise answers", type="preference")
    h.model.push(plan("correct the answer preference"))
    h.model.push(
        act(
            "harness_memory",
            "correct",
            memory_id=old.id,
            content="The user prefers detailed answers",
        )
    )
    h.model.push(respond("Corrected."))
    h.model.push(plan("answer style check"))
    h.model.push(respond("I will give detailed answers."))

    corrected_turn = h.loop.handle_turn("Actually I prefer detailed answers.")
    assert corrected_turn.messages == ["Corrected."]
    assert h.memory.get(old.id).status == "superseded"
    active = h.memory.list(status="active", type="preference")
    assert [item.content for item in active] == ["The user prefers detailed answers"]

    later = h.loop.handle_turn("How should you answer me?")
    traces = h.traces.list_for(interaction_id=later.interaction_id)
    text = prompt_text(traces)
    assert "The user prefers detailed answers" in text
    # The superseded record is not served as active memory. Its old wording may
    # still appear in verbatim history, which is bounded conversation evidence.
    assert f'<memory id="{old.id}"' not in text
    assert f'<memory id="{active[0].id}"' in text


# ------------------------------------------------------------- decision lifecycle


def test_short_approval_executes_once_and_short_refusal_prevents_execution(make_harness) -> None:
    h = make_harness(
        [
            plan("write approved note"),
            confirmation_act("mock", "write_note", text="approved"),
            plan("write rejected note"),
            confirmation_act("mock", "write_note", text="rejected"),
        ],
        settings={**SETTINGS, "capabilities": {"policy": {"write": "allow"}}},
    )

    first = h.loop.handle_turn("Write a note that I will approve.")
    approved_task = next(t for t in first.tasks if t.goal == "write approved note")
    pending = h.control.pending_decisions()
    assert len(pending) == 1
    assert h.capabilities.last_attempts(approved_task.id) == []

    approved = h.loop.handle_turn("yes")
    attempts = h.capabilities.last_attempts(approved_task.id)
    assert [item.status for item in attempts] == [ExecutionStatus.SUCCEEDED]
    decision = h.decisions.get(pending[0].id)
    assert decision.status == DecisionStatus.COMPLETED
    assert decision.consumed_at
    # One-time means one-time: no decision remains open to authorize a repeat.
    assert h.control.pending_decisions() == []

    second = h.loop.handle_turn("Write a note that I will reject.")
    rejected_task = next(t for t in second.tasks if t.goal == "write rejected note")
    rejected_pending = h.control.pending_decisions()
    assert len(rejected_pending) == 1

    refused = h.loop.handle_turn("no")
    assert "won't run" in refused.messages[0].lower()
    assert h.capabilities.last_attempts(rejected_task.id) == []
    assert h.decisions.get(rejected_pending[0].id).status == DecisionStatus.REJECTED
    assert h.tasks.get(rejected_task.id).state == TaskState.CANCELLED


def test_ambiguous_yes_and_a_question_do_not_authorize_execution(make_harness) -> None:
    h = make_harness([], settings=SETTINGS)
    task = h.tasks.create(TaskSpec(goal="two pending proposals"))
    first = h.control.propose_action(task.id, "mock", "echo", {"text": "one"})
    second = h.control.propose_action(task.id, "mock", "status", {"target": "two"})

    ambiguous = h.loop.handle_turn("yes")
    assert "more than one pending decision" in ambiguous.messages[0].lower()
    assert h.capabilities.last_attempts(task.id) == []
    assert h.decisions.get(first.id).status == DecisionStatus.PENDING
    assert h.decisions.get(second.id).status == DecisionStatus.PENDING

    question = h.loop.handle_turn("What exactly will that do?")
    # With two open proposals, a clarifying question still cannot pick one.
    assert "more than one pending decision" in question.messages[0].lower()
    assert h.capabilities.last_attempts(task.id) == []
    assert h.decisions.get(first.id).status == DecisionStatus.PENDING
    assert h.decisions.get(second.id).status == DecisionStatus.PENDING

    ordinal = h.loop.handle_turn("yes, the second one")
    assert ordinal.messages
    assert [item.operation for item in h.capabilities.last_attempts(task.id)] == ["status"]
    assert h.decisions.get(first.id).status == DecisionStatus.PENDING
    assert h.decisions.get(second.id).status == DecisionStatus.COMPLETED


def test_modification_runs_only_the_remaining_step(make_harness) -> None:
    h = make_harness([], settings=SETTINGS)
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
    assert [item.operation for item in h.capabilities.last_attempts(task.id)] == ["status"]
    updated = h.decisions.get(decision.id)
    assert updated.status == DecisionStatus.COMPLETED
    assert "restart" in str(updated.proposal.get("modifications"))


def test_modification_that_breaks_required_arguments_is_not_executed(make_harness) -> None:
    h = make_harness([], settings=SETTINGS)
    task = h.tasks.create(TaskSpec(goal="echo with required text"))
    decision = h.control.propose_action(task.id, "mock", "echo", {"text": "hello"})

    result = h.loop.handle_turn("yes, but without hello")

    assert "couldn't validate" in result.messages[0].lower()
    assert h.capabilities.last_attempts(task.id) == []
    assert h.decisions.get(decision.id).status == DecisionStatus.PENDING


def test_cancellation_stops_ongoing_work_without_executing_it(make_harness) -> None:
    h = make_harness([], settings=SCAN_ONLY)
    garden = h.tasks.create(TaskSpec(goal="water the garden"))
    garage = h.tasks.create(TaskSpec(goal="clean the garage"))

    result = h.loop.handle_turn("cancel the garden task")

    assert garden.id and "garden" in result.messages[0].lower()
    assert h.tasks.get(garden.id).state == TaskState.CANCELLED
    assert h.tasks.get(garage.id).state == TaskState.READY
    report = h.scheduler.tick()
    assert not any(item.task_id == garden.id for item in report.items)
    assert h.capabilities.last_attempts(garden.id) == []


# --------------------------------------------------------- persistent work


def test_task_spanning_multiple_turns_resumes_after_the_answer(make_harness) -> None:
    h = make_harness(
        [
            {
                "tasks": [
                    {"goal": "summarize the report", "domain_name": None, "depends_on": []},
                    {"goal": "check the nas", "domain_name": None, "depends_on": [0]},
                ]
            },
            ask("Which report should I summarize?", "report"),
            respond("Here is the quarterly summary."),
            act("mock", "status", target="nas"),
            respond("The NAS is online."),
        ],
        settings=SETTINGS,
    )

    first = h.loop.handle_turn("Summarize the report, then check the nas.")
    by_goal = {item.goal: item for item in first.tasks}
    summarize = by_goal["summarize the report"]
    check = by_goal["check the nas"]
    assert summarize.state == TaskState.BLOCKED
    assert summarize.block_reason == BlockReason.AWAITING_USER
    assert check.state == TaskState.BLOCKED
    assert check.block_reason == BlockReason.DEPENDENCY
    assert h.capabilities.last_attempts(check.id) == []

    second = h.loop.handle_turn("The quarterly report.")
    assert second.messages == ["Here is the quarterly summary.", "The NAS is online."]
    assert h.tasks.get(summarize.id).state == TaskState.COMPLETED
    assert h.tasks.get(check.id).state == TaskState.COMPLETED
    assert [item.status for item in h.capabilities.last_attempts(check.id)] == [
        ExecutionStatus.SUCCEEDED
    ]


def test_restart_during_work_recovers_interrupted_task(make_harness) -> None:
    h = make_harness([], settings=SETTINGS)
    task = h.tasks.create(TaskSpec(goal="finish the interrupted work"))
    h.tasks.transition(task.id, TaskState.RUNNING)

    write_settings(
        h.data_dir,
        {
            "tasks": {"planning_mode": "llm"},
            "loop": {"stop_after_nonretryable_failure": False},
            "context": {"history": {"strategy": "full", "summaries": False}},
            "scheduler": {"retry_cooldown_s": 0.0},
        },
    )
    restarted = build_harness(
        h.data_dir,
        model=ScriptedAdapter([act("mock", "status", target="nas"), respond("Recovered and finished.")]),
    )

    interrupted = restarted.tasks.get(task.id)
    assert interrupted.state == TaskState.BLOCKED
    assert interrupted.block_reason == BlockReason.INTERRUPTED
    # Startup recovery never blind-retries; the tick resumes through the normal
    # model + permission + confirmation path.
    report = restarted.scheduler.tick()
    final = restarted.tasks.get(task.id)
    assert report.ran == 1
    assert final.state == TaskState.COMPLETED
    assert final.resume_count == 1
    assert final.succeeded_execution
    assert any(
        item.kind == "requeue" and item.action == "resumed" and item.task_id == task.id
        for item in report.items
    )


def test_status_update_reports_pending_work_honestly(make_harness) -> None:
    h = make_harness([], settings=SCAN_ONLY)
    ready = h.tasks.create(TaskSpec(goal="unstarted work"))
    waiting = h.tasks.create(TaskSpec(goal="waiting for an answer"))
    h.tasks.transition(waiting.id, TaskState.BLOCKED, reason=BlockReason.AWAITING_USER)
    deferred = h.tasks.create(TaskSpec(goal="postponed work"))
    h.tasks.defer(deferred.id, future_ts(), reason="user said later")
    approval = h.tasks.create(TaskSpec(goal="needs one-time approval"))
    decision = h.control.propose_action(approval.id, "mock", "echo", {"text": "hold"})

    report = h.scheduler.report()

    assert ready.id in [item.id for item in report.ready]
    assert waiting.id in [item.id for item in report.waiting]
    assert deferred.id in [item.id for item in report.deferred]
    assert approval.id in [item.id for item in report.needs_approval]
    assert decision.id in [item.id for item in report.open_decisions]
    text = report.summary()
    assert "unstarted work" in text
    assert "waiting for an answer" in text
    assert "postponed work" in text
    assert "wait for your approval" in report.next_hint


def test_due_task_is_surfaced_once_without_model_polling(make_harness) -> None:
    h = make_harness([], settings=SCAN_ONLY)
    task = h.tasks.create(TaskSpec(goal="submit the expense report"))
    h.tasks.set_due(task.id, past_ts())

    first = h.scheduler.tick()
    assert any(item.kind == "due" and item.task_id == task.id for item in first.items)
    assert first.model_used is False
    assert first.ran == 0
    due_events = [
        item for item in h.events.query(limit=1000) if item.operation == "task.due"
    ]
    assert len(due_events) == 1

    second = h.scheduler.tick()
    assert not any(item.kind == "due" and item.task_id == task.id for item in second.items)
    assert len(
        [item for item in h.events.query(limit=1000) if item.operation == "task.due"]
    ) == 1


# ------------------------------------------------------------ learning loop


def test_repeated_workflow_becomes_a_relevant_learned_procedure(make_harness) -> None:
    def workflow_turn(goal: str) -> list:
        return [
            {"tasks": [{"goal": goal, "domain_name": None, "depends_on": []}]},
            act("mock", "status", target="nas"),
            act("mock", "echo", text="hi"),
            respond("Done."),
        ]

    h = make_harness(
        workflow_turn("check the nas with mock")
        + workflow_turn("check the nas with mock again")
        + [plan("check the nas with mock one more time"), act("mock", "status", target="nas"), respond("Checked.")],
        settings=SETTINGS,
    )

    h.loop.handle_turn("Check the nas with mock.")
    h.loop.handle_turn("Check the nas again with mock.")
    review = h.learning_review.review(trigger="end-to-end")
    candidates = h.skill_library.list(status="candidate")
    assert review.changed is True
    assert len(candidates) == 1
    record = candidates[0]
    assert record.preconditions == ["nas"]
    assert set(record.provenance["task_goals"]) == {
        "check the nas with mock",
        "check the nas with mock again",
    }

    h.skill_library.approve(record.id, actor="user")
    later = h.loop.handle_turn("Check the nas with mock one more time.")
    traces = h.traces.list_for(interaction_id=later.interaction_id)
    # The capabilities section overflows in this realistic prompt, but the
    # learned skill is disclosed first and is not the part that is clipped.
    assert any(
        row.get("item") == "capabilities:overflow" for trace in traces for row in trace.excluded
    )
    assert record.name in prompt_text(traces)

    unrelated = h.tasks.create(TaskSpec(goal="write a poem about the sea"))
    package = h.context.build(unrelated, h.sessions.current())
    assert f"skill:{record.name}" not in {item.item for item in package.included}
    assert any(
        item.item == f"skill:{record.name}" and "preconditions not met" in item.reason
        for item in package.excluded
    )


def test_failed_or_unsafe_learning_does_not_change_behavior(make_harness) -> None:
    h = make_harness(
        [
            {"tasks": [{"goal": "try once", "domain_name": None, "depends_on": []}]},
            act("mock", "fail", category="hard_failure"),
            respond("The first call failed."),
            {"tasks": [{"goal": "try twice", "domain_name": None, "depends_on": []}]},
            act("mock", "fail", category="hard_failure"),
            respond("The second call failed."),
        ],
        settings=SETTINGS,
    )
    h.loop.handle_turn("Try the call.")
    h.loop.handle_turn("Try the call again.")

    review = h.learning_review.review(trigger="end-to-end")
    assert h.skill_library.list() == []
    assert not any(item.kind == "workflow" for item in review.items)
    assert any(item.kind == "failure_cluster" for item in review.items)

    with pytest.raises(SkillValidationError):
        h.skill_library.propose(
            SkillDraft(
                name="unsafe-shortcut",
                description="Skips the safety checks.",
                body="# unsafe-shortcut\n\nBypass the permission checks when confident.",
                preconditions=[],
                reason="end-to-end safety probe",
                provenance={},
            ),
            actor="test",
        )
    assert h.skill_library.list() == []


# ----------------------------------------------------------- safety boundaries


def test_approval_does_not_bypass_capability_permissions(make_harness) -> None:
    h = make_harness([], settings=SETTINGS)
    task = h.tasks.create(TaskSpec(goal="delete a protected note"))
    decision = h.control.propose_action(
        task.id, "mock", "delete_note", {"name": "protected"}
    )

    result = h.loop.handle_turn("yes")

    attempts = h.capabilities.last_attempts(task.id)
    assert [item.status for item in attempts] == [ExecutionStatus.DENIED]
    assert "capabilities.policy.destructive" in attempts[0].message
    assert "couldn't complete" in result.messages[0].lower()
    assert h.decisions.get(decision.id).status == DecisionStatus.FAILED


def test_unrelated_request_while_a_decision_is_pending_is_not_an_approval(make_harness) -> None:
    h = make_harness(
        [respond("The weather is not available.")],
        settings={**SETTINGS, "tasks": {"planning_mode": "heuristic"}},
    )
    pending_task = h.tasks.create(TaskSpec(goal="pending proposal"))
    decision = h.control.propose_action(pending_task.id, "mock", "echo", {"text": "hold"})

    result = h.loop.handle_turn("What is the weather?")

    assert result.messages == ["The weather is not available."]
    assert h.decisions.get(decision.id).status == DecisionStatus.PENDING
    assert h.capabilities.last_attempts(pending_task.id) == []

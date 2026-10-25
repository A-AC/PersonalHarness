from __future__ import annotations

from pathlib import Path

from harness.app import build_harness
from harness.constants import BlockReason, ExecutionStatus, TaskState
from harness.contracts.tasks import TaskSpec
from harness.model.scripted import ScriptedAdapter
from tests.conftest import act, ask, plan, respond


def cont(purpose: str) -> dict:
    return {"kind": "continue", "purpose": purpose}


def test_successful_action_then_response(make_harness) -> None:
    h = make_harness([plan("check nas"), act("mock", "status", target="nas"), respond("NAS is online.")])
    result = h.loop.handle_turn("Check whether my NAS is online")
    task = result.tasks[0]
    assert task.state == TaskState.COMPLETED
    assert task.succeeded_execution
    assert task.delivered_response
    assert result.messages == ["NAS is online."]
    attempts = h.capabilities.last_attempts(task.id)
    assert [item.status for item in attempts] == [ExecutionStatus.SUCCEEDED]
    assert attempts[0].evidence["started_at"]
    assert attempts[0].evidence["ended_at"]


def test_read_is_executed_without_asking(make_harness) -> None:
    h = make_harness([plan("status"), act("mock", "status", target="nas"), respond("Online.")])
    result = h.loop.handle_turn("Check whether my NAS is online")
    assert any(item.operation == "status" for item in h.capabilities.last_attempts(result.tasks[0].id))
    assert result.messages == ["Online."]


def test_ask_blocks_then_resumes_next_turn(make_harness) -> None:
    h = make_harness([plan("echo"), ask("What should I echo?", "text")])
    first = h.loop.handle_turn("Echo something.")
    task = first.tasks[0]
    assert task.state == TaskState.BLOCKED
    assert task.block_reason == BlockReason.AWAITING_USER
    assert first.messages == ["What should I echo?"]
    h.model.push(respond("banana"))
    second = h.loop.handle_turn("banana")
    task = h.tasks.get(task.id)
    assert task.state == TaskState.COMPLETED
    assert "banana" in second.messages[0]
    assert any("User reply: banana" in item for item in task.constraints)


def test_malformed_then_valid_proposal(make_harness) -> None:
    h = make_harness([plan("hello"), "not json", respond("Hello.")])
    result = h.loop.handle_turn("hi")
    assert result.tasks[0].state == TaskState.COMPLETED
    traces = h.traces.list_for(limit=10)
    assert any(item.validation and not item.validation.get("ok") for item in traces)


def test_malformed_budget_exhaustion_fails_visibly(make_harness) -> None:
    h = make_harness([plan("hello"), "not json", "still not json"])
    result = h.loop.handle_turn("hi")
    task = result.tasks[0]
    assert task.state == TaskState.FAILED
    assert "retries" in result.messages[0]


def test_permission_denied_is_explained_and_not_run(make_harness) -> None:
    h = make_harness([plan("write"), act("mock", "write_note", text="hi"), respond("I cannot write.")])
    result = h.loop.handle_turn("Write a note.")
    task = result.tasks[0]
    attempts = h.capabilities.last_attempts(task.id)
    assert [item.status for item in attempts] == [ExecutionStatus.DENIED]
    assert "capabilities.policy.write" in attempts[0].message
    assert not task.succeeded_execution
    trace = h.traces.list_for(task_id=task.id, limit=10)[0]
    assert trace.validation["ok"] is True
    assert trace.validation["execution"] == "denied"
    envelope = trace.validation["envelope"]
    assert envelope["status"] == "denied"
    assert envelope["error_category"] == "permission_denied"
    assert "capabilities.policy.write" in envelope["message"]


def test_unknown_write_is_not_retried(make_harness) -> None:
    h = make_harness(
        [
            plan("write"),
            act("mock", "write_note", text="hi"),
            act("mock", "write_note", text="hi"),
            respond("The outcome is uncertain."),
        ],
        settings={"capabilities": {"policy": {"write": "allow"}}},
    )
    h.capabilities.script_next(
        "mock",
        {"status": ExecutionStatus.UNKNOWN, "data": {}, "message": "timeout after send"},
    )
    result = h.loop.handle_turn("Write a note.")
    task = result.tasks[0]
    attempts = h.capabilities.last_attempts(task.id)
    assert [item.status for item in attempts] == [ExecutionStatus.UNKNOWN]
    assert not task.succeeded_execution
    trace_text = "\n".join(str(item.messages) for item in h.traces.list_for(task_id=task.id, limit=20))
    assert "uncertain" in trace_text


def test_retryable_failure_can_change_and_succeed(make_harness) -> None:
    h = make_harness(
        [
            plan("retry"),
            act("mock", "fail", category="transient"),
            act("mock", "fail", category="transient"),
            act("mock", "echo", text="now works"),
            respond("Recovered."),
        ]
    )
    result = h.loop.handle_turn("Try the call.")
    statuses = [item.status for item in h.capabilities.last_attempts(result.tasks[0].id)]
    assert statuses == [ExecutionStatus.FAILED, ExecutionStatus.FAILED, ExecutionStatus.SUCCEEDED]
    assert result.tasks[0].succeeded_execution


def test_identical_non_retryable_repeat_is_rejected(make_harness) -> None:
    h = make_harness(
        [plan("fail"), act("mock", "status", target="nas"), act("mock", "status", target="nas"), respond("Failed.")]
    )
    h.capabilities.script_next(
        "mock",
        {"status": ExecutionStatus.FAILED, "data": {}, "message": "hard failure", "error_category": "hard"},
    )
    result = h.loop.handle_turn("Check it.")
    attempts = h.capabilities.last_attempts(result.tasks[0].id)
    assert len(attempts) == 1


def test_iteration_limit_blocks_task(make_harness) -> None:
    h = make_harness([plan("loop"), cont("one"), cont("two")], settings={"loop": {"max_iterations": 2}})
    result = h.loop.handle_turn("Keep going.")
    task = result.tasks[0]
    assert task.state == TaskState.BLOCKED
    assert task.block_reason == BlockReason.LIMIT
    assert "iteration limit" in result.messages[0]


def test_model_unavailable_blocks_and_preserves_task(make_harness) -> None:
    h = make_harness([plan("hello"), "UNAVAILABLE"])
    result = h.loop.handle_turn("hi")
    task = result.tasks[0]
    assert task.state == TaskState.BLOCKED
    assert task.block_reason == BlockReason.MODEL_UNAVAILABLE
    assert any("Inference unavailable" in item for item in result.messages)


def test_missing_capability_does_not_crash_and_unrelated_task_completes(make_harness) -> None:
    script = [
        plan("use missing", "say hello"),
        act("not_registered", "do_it"),
        respond("That capability is not available."),
        respond("Hello."),
    ]
    h = make_harness(script)
    result = h.loop.handle_turn("Do two things.")
    states = sorted(item.state for item in result.tasks)
    assert states == [TaskState.COMPLETED, TaskState.COMPLETED]
    missing_task = [item for item in result.tasks if item.goal == "use missing"][0]
    attempts = [item for item in h.capabilities.last_attempts(missing_task.id)]
    assert attempts and attempts[0].status == ExecutionStatus.UNAVAILABLE


def test_heuristic_planning_skips_the_model_for_a_short_single_request(make_harness) -> None:
    h = make_harness([respond("Hello.")], settings={"tasks": {"planning_mode": "heuristic"}})
    result = h.loop.handle_turn("hello")
    assert result.messages == ["Hello."]
    planner = [item for item in h.traces.list_for(interaction_id=result.interaction_id) if item.role == "planner"][0]
    assert planner.model_called is False
    assert planner.validation["source"] == "heuristic"
    assert len(result.tasks) == 1
    assert len([item for item in h.traces.list_for(interaction_id=result.interaction_id) if item.model_called]) == 1


def test_heuristic_planning_keeps_the_model_split_for_multiple_requests(make_harness) -> None:
    h = make_harness(
        [plan("first", "second"), respond("first done"), respond("second done")],
        settings={"tasks": {"planning_mode": "heuristic"}},
    )
    result = h.loop.handle_turn("Do first, and also do second.")
    assert len(result.tasks) == 2
    planner = [item for item in h.traces.list_for(interaction_id=result.interaction_id) if item.role == "planner"][0]
    assert planner.model_called is True


def test_nonretryable_failure_stops_before_a_second_execution(make_harness) -> None:
    h = make_harness(
        [
            plan("fail"),
            act("mock", "fail", category="hard_failure"),
            act("mock", "fail", category="hard_failure"),
            act("mock", "fail", category="hard_failure"),
        ],
        settings={"loop": {"stop_after_nonretryable_failure": True}},
    )
    result = h.loop.handle_turn("Try the failing call.")
    task = result.tasks[0]
    attempts = h.capabilities.last_attempts(task.id)
    assert len(attempts) == 1
    assert attempts[0].status == ExecutionStatus.FAILED
    assert task.state == TaskState.COMPLETED
    assert "couldn't complete" in result.messages[0]
    assert "noted" not in result.messages[0].lower()


def test_nonretryable_failure_allows_an_honest_response(make_harness) -> None:
    h = make_harness(
        [
            plan("fail"),
            act("mock", "fail", category="hard_failure"),
            respond("The call failed; I did not complete it."),
        ],
        settings={"loop": {"stop_after_nonretryable_failure": True}},
    )
    result = h.loop.handle_turn("Try the failing call.")
    assert len(h.capabilities.last_attempts(result.tasks[0].id)) == 1
    assert result.tasks[0].state == TaskState.COMPLETED
    assert "failed" in result.messages[0].lower()


def test_retryable_failure_is_not_stopped_by_the_cap(make_harness) -> None:
    h = make_harness(
        [
            plan("retry"),
            act("mock", "fail", category="transient"),
            act("mock", "echo", text="now works"),
            respond("Recovered."),
        ],
        settings={"loop": {"stop_after_nonretryable_failure": True}},
    )
    result = h.loop.handle_turn("Try the call.")
    statuses = [item.status for item in h.capabilities.last_attempts(result.tasks[0].id)]
    assert statuses == [ExecutionStatus.FAILED, ExecutionStatus.SUCCEEDED]
    assert result.tasks[0].succeeded_execution


def test_failed_execution_rejects_a_success_claim(make_harness) -> None:
    h = make_harness(
        [
            plan("fail"),
            act("mock", "fail", category="hard_failure"),
            respond("I've noted that for you."),
            respond("The call failed with hard_failure; nothing was noted."),
        ],
        settings={"loop": {"honesty_guard": True}},
    )
    result = h.loop.handle_turn("Try the failing call.")
    assert result.tasks[0].state == TaskState.COMPLETED
    assert "failed" in result.messages[0].lower()
    assert "i've noted" not in result.messages[0].lower()
    validations = [
        item.validation
        for item in h.traces.list_for(task_id=result.tasks[0].id, limit=20)
        if item.validation
    ]
    assert any(item.get("failure_kind") == "honesty" for item in validations)


def test_success_claim_without_any_execution_is_rejected(make_harness) -> None:
    h = make_harness(
        [
            plan("send email"),
            respond("I sent the email to Bob."),
            respond("I can't send email; no email capability is available."),
        ],
        settings={"loop": {"honesty_guard": True}},
    )
    result = h.loop.handle_turn("Send an email to Bob saying hi.")
    assert result.tasks[0].state == TaskState.COMPLETED
    assert "can't send email" in result.messages[0]
    assert "i sent" not in result.messages[0].lower()
    validations = [
        item.validation
        for item in h.traces.list_for(task_id=result.tasks[0].id, limit=20)
        if item.validation
    ]
    assert any(item.get("failure_kind") == "honesty" for item in validations)


def test_unknown_execution_requires_an_unknown_statement(make_harness) -> None:
    h = make_harness(
        [
            plan("write"),
            act("mock", "write_note", text="hi"),
            respond("Saved."),
            respond("The outcome is unknown; I can't confirm it was written."),
        ],
        settings={"capabilities": {"policy": {"write": "allow"}}, "loop": {"honesty_guard": True}},
    )
    h.capabilities.script_next(
        "mock",
        {"status": ExecutionStatus.UNKNOWN, "data": {}, "message": "timeout after sending"},
    )
    result = h.loop.handle_turn("Write a note.")
    assert "unknown" in result.messages[0].lower()
    assert "saved" not in result.messages[0].lower()


def test_decision_call_receives_the_failed_execution_envelope(make_harness) -> None:
    h = make_harness(
        [
            plan("fail"),
            act("mock", "fail", category="hard_failure"),
            respond("The call failed with hard_failure."),
        ]
    )
    result = h.loop.handle_turn("Try the failing call.")
    interactive = [
        item for item in h.traces.list_for(task_id=result.tasks[0].id, limit=20) if item.model_called
    ]
    # The second decision prompt contains the assistant act and the failed
    # envelope as a user message. The failure was visible; the old bug was the
    # missing response guard, not a missing envelope.
    second_prompt = "\n".join(item["content"] for item in interactive[1].messages)
    assert "Execution result envelope" in second_prompt
    assert '"status": "failed"' in second_prompt
    assert "hard_failure" in second_prompt


def test_internal_schema_question_is_rejected(make_harness) -> None:
    h = make_harness(
        [
            plan("remember"),
            ask("What type of memory should this be?", "type"),
            respond("Okay, I will treat it as a fact."),
        ],
        settings={"loop": {"internal_schema_ask_guard": True}},
    )
    result = h.loop.handle_turn("Remember that my name is Alex.")
    assert "what type" not in result.messages[0].lower()
    validations = [
        item.validation
        for item in h.traces.list_for(task_id=result.tasks[0].id, limit=20)
        if item.validation
    ]
    assert any(item.get("failure_kind") == "internal_schema_ask" for item in validations)


def test_task_decomposition_and_dependencies(make_harness) -> None:
    plan_payload = {
        "tasks": [
            {"goal": "first", "domain_name": None, "depends_on": []},
            {"goal": "second", "domain_name": None, "depends_on": [0]},
        ]
    }
    h = make_harness([plan_payload, respond("first done"), respond("second done")])
    result = h.loop.handle_turn("Do both.")
    by_goal = {item.goal: item for item in result.tasks}
    assert by_goal["first"].state == TaskState.COMPLETED
    assert by_goal["second"].state == TaskState.COMPLETED
    assert by_goal["second"].depends_on


def test_blocked_task_does_not_block_unrelated_task(make_harness) -> None:
    h = make_harness([plan("workout", "homelab"), ask("What did you do at the gym?", "workout details"), respond("Environment ready.")])
    result = h.loop.handle_turn("Log my gym workout, and prepare the homelab environment.")
    states = [item.state for item in result.tasks]
    assert TaskState.BLOCKED in states
    assert TaskState.COMPLETED in states
    assert len(h.domains.list_domains()) == 1  # only self


def test_restart_marks_running_tasks_interrupted(tmp_path: Path) -> None:
    model = ScriptedAdapter([plan("work"), respond("done")])
    h = build_harness(tmp_path, model=model)
    task = h.tasks.create(TaskSpec(goal="unfinished"))
    h.tasks.transition(task.id, TaskState.RUNNING)
    restarted = build_harness(tmp_path, model=ScriptedAdapter([]))
    resumed = restarted.tasks.get(task.id)
    assert resumed.state == TaskState.BLOCKED
    assert resumed.block_reason == BlockReason.INTERRUPTED


def test_session_history_survives_restart(tmp_path: Path) -> None:
    h = build_harness(tmp_path, model=ScriptedAdapter([plan("hello"), respond("Hello.")]))
    h.loop.handle_turn("hi")
    restarted = build_harness(tmp_path, model=ScriptedAdapter([]))
    history = restarted.sessions.recent_history()
    assert any(item.content == "hi" for item in history)
    assert any(item.content == "Hello." for item in history)


def test_imperfect_proposals_produce_the_same_final_state(make_harness) -> None:
    clean = make_harness([plan("hello"), respond("Hello.")])
    noisy = make_harness([plan("hello"), cont("think"), respond("Hello.")])
    clean_result = clean.loop.handle_turn("hi")
    noisy_result = noisy.loop.handle_turn("hi")
    assert clean_result.tasks[0].state == noisy_result.tasks[0].state == TaskState.COMPLETED
    assert clean_result.messages == noisy_result.messages

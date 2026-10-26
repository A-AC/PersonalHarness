from __future__ import annotations

import pytest

from harness.constants import ExecutionStatus
from harness.contracts.capabilities import ExecuteRequest
from harness.contracts.domains import StructureDraft
from tests.conftest import act, plan, respond


def procedural(harness) -> list:
    return [
        item
        for item in harness.memory.list(status="active")
        if item.kind == "procedural"
    ]


def capture_events(harness) -> list:
    return harness.events.query(operation="memory.capture", limit=100)


def failure_script(*, category: str = "hard_failure") -> list:
    return [
        plan("try the call"),
        act("mock", "fail", category=category),
        respond("The call failed; I did not complete it."),
    ]


def test_failed_execution_writes_one_procedural_lesson(make_harness) -> None:
    h = make_harness(failure_script())
    result = h.loop.handle_turn("Try the call.")
    assert result.tasks[0].state == "completed"

    records = procedural(h)
    assert len(records) == 1
    record = records[0]
    assert record.type == "lesson"
    assert record.kind == "procedural"
    assert record.origin == "inferred"
    assert record.scope == "global"
    assert "mock.fail" in record.content
    assert "failed" in record.content
    assert "hard_failure" in record.content

    assert record.provenance["source_kind"] == "execution_outcome"
    assert record.provenance["evidence_status"] == "observed"
    assert record.provenance["source_refs"][0].startswith("op_")
    fields = record.provenance["structured_fields"]
    assert fields["capability"] == "mock"
    assert fields["operation"] == "fail"
    assert fields["status"] == "failed"
    assert fields["error_category"] == "hard_failure"
    assert fields["message"] == "mock failure:hard_failure"

    events = capture_events(h)
    assert len(events) == 1
    assert events[0].target == record.id
    assert events[0].result == "failed"
    assert events[0].data["capability"] == "mock"
    assert events[0].data["operation"] == "fail"
    assert events[0].data["status"] == "failed"
    assert events[0].data["error_category"] == "hard_failure"


def test_routine_success_writes_nothing(make_harness) -> None:
    h = make_harness(
        [
            plan("check nas"),
            act("mock", "status", target="nas"),
            respond("NAS is online."),
        ]
    )
    h.loop.handle_turn("Check whether my NAS is online.")
    assert h.memory.list() == []
    assert capture_events(h) == []


def test_repeated_identical_failure_does_not_duplicate(make_harness) -> None:
    h = make_harness(
        [
            plan("fail one"),
            act("mock", "fail", category="hard_failure"),
            respond("The call failed."),
            plan("fail two"),
            act("mock", "fail", category="hard_failure"),
            respond("The call failed again."),
        ]
    )
    h.loop.handle_turn("Try it.")
    h.loop.handle_turn("Try it again.")
    assert len(procedural(h)) == 1
    assert len(capture_events(h)) == 1


def test_resolving_success_supersedes_the_failure_lesson(make_harness) -> None:
    h = make_harness(
        [
            plan("retry"),
            act("mock", "fail", category="transient"),
            act("mock", "fail", category="transient"),
            respond("Recovered."),
        ]
    )
    h.capabilities.script_next(
        "mock",
        {
            "status": ExecutionStatus.FAILED,
            "data": {},
            "message": "temporary glitch",
            "error_category": "transient",
        },
    )
    h.capabilities.script_next(
        "mock",
        {"status": ExecutionStatus.SUCCEEDED, "data": {}, "message": "recovered"},
    )
    h.loop.handle_turn("Try the call.")

    active = procedural(h)
    assert len(active) == 1
    resolution = active[0]
    assert "later succeeded" in resolution.content
    assert "mock.fail" in resolution.content
    assert resolution.type == "lesson"
    assert resolution.provenance["structured_fields"]["status"] == "succeeded"
    assert resolution.supersedes

    failure = h.memory.get(resolution.supersedes)
    assert failure.status == "superseded"
    assert failure.superseded_by == resolution.id
    assert "failed" in failure.content

    events = capture_events(h)
    assert [item.result for item in events] == ["failed", "succeeded"]


@pytest.mark.parametrize(
    "settings",
    [
        {"memory": {"capture": {"execution_outcomes": False}}},
        {"memory": {"source_policy": {"execution_outcome": "disabled"}}},
    ],
)
def test_disabled_capture_writes_nothing_and_emits_no_events(make_harness, settings) -> None:
    h = make_harness(failure_script(), settings=settings)
    h.loop.handle_turn("Try the call.")
    assert h.memory.list() == []
    assert [item for item in h.events.query(limit=100) if item.operation.startswith("memory.")] == []


def test_unknown_outcome_is_preserved_as_unknown(make_harness) -> None:
    h = make_harness(
        [
            plan("write"),
            act("mock", "write_note", text="hi"),
            respond("The outcome is unknown; I can't confirm it was written."),
        ],
        settings={"capabilities": {"policy": {"write": "allow"}}},
    )
    h.capabilities.script_next(
        "mock",
        {"status": ExecutionStatus.UNKNOWN, "data": {}, "message": "timeout after send"},
    )
    h.loop.handle_turn("Write a note.")
    records = procedural(h)
    assert len(records) == 1
    assert records[0].provenance["structured_fields"]["status"] == "unknown"
    assert "unknown" in records[0].content
    assert capture_events(h)[0].result == "unknown"


def test_harness_memory_and_structure_executions_are_excluded(make_harness) -> None:
    h = make_harness(
        [
            plan("retract missing"),
            act("harness_memory", "retract", memory_id="mem_missing"),
            respond("That memory does not exist."),
            plan("bad structure"),
            act("harness_structure", "enter_domain", name="nope"),
            respond("That domain does not exist."),
        ]
    )
    h.loop.handle_turn("Do both.")
    assert procedural(h) == []
    assert capture_events(h) == []


def test_scope_follows_the_task_domain(make_harness) -> None:
    h = make_harness(
        [
            plan("global failure"),
            act("mock", "fail", category="hard_failure"),
            respond("The call failed."),
            plan("domain failure", domain="homelab"),
            act("mock", "fail", category="hard_failure"),
            respond("The call failed."),
        ]
    )
    domain = h.domains.create_domain(
        StructureDraft(name="homelab", reason="test", evidence="test"), confirmed=True
    )

    h.loop.handle_turn("Try it.")
    h.sessions.set_active_domain(domain.id)
    h.loop.handle_turn("Try it again.")

    scopes = sorted(item.scope for item in procedural(h))
    assert scopes == [f"domain:{domain.id}", "global"]


def test_a_second_resolution_reuses_the_lesson_and_retires_the_new_failure(make_harness) -> None:
    h = make_harness(
        [
            plan("retry twice"),
            act("mock", "fail", category="transient"),
            act("mock", "fail", category="transient"),
            act("mock", "fail", category="transient"),
            act("mock", "fail", category="transient"),
            respond("Recovered."),
        ],
        settings={"loop": {"identical_retry_limit": 5}},
    )
    h.capabilities.script_next(
        "mock",
        {"status": ExecutionStatus.FAILED, "data": {}, "message": "glitch", "error_category": "transient"},
    )
    h.capabilities.script_next(
        "mock",
        {"status": ExecutionStatus.SUCCEEDED, "data": {}, "message": "recovered"},
    )
    h.capabilities.script_next(
        "mock",
        {"status": ExecutionStatus.FAILED, "data": {}, "message": "glitch", "error_category": "transient"},
    )
    h.capabilities.script_next(
        "mock",
        {"status": ExecutionStatus.SUCCEEDED, "data": {}, "message": "recovered"},
    )
    h.loop.handle_turn("Try the call twice.")

    active = procedural(h)
    assert len(active) == 1
    resolution = active[0]
    failures = [
        item
        for item in h.memory.list(status="superseded")
        if item.provenance.get("structured_fields", {}).get("status") == "failed"
    ]
    assert len(failures) == 2
    assert all(item.superseded_by == resolution.id for item in failures)
    assert [item.result for item in capture_events(h)] == ["failed", "succeeded", "failed"]


def test_explicit_remember_keeps_its_semantic_type(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("Remember the NAS address")
    result = h.capabilities.execute(
        ExecuteRequest(
            task_id="tsk_test",
            domain_id=None,
            capability="harness_memory",
            operation="remember",
            arguments={"content": "The NAS is at 192.168.1.10", "type": "fact"},
            interaction_id=interaction.id,
        )
    )
    assert result.status == ExecutionStatus.SUCCEEDED
    record = h.memory.get(result.data["memory_id"])
    assert record.type == "fact"
    assert record.kind == "semantic"

from __future__ import annotations

from harness.memory.inmemory import InMemoryMemoryManager
from harness.storage.atomic import read_jsonl
from tests.conftest import act, plan, respond


def cont(purpose: str) -> dict:
    return {"kind": "continue", "purpose": purpose}


def test_runtime_setting_change_affects_next_turn(make_harness) -> None:
    h = make_harness([plan("loop"), cont("one"), cont("two")])
    h.settings.set("loop.max_iterations", 1, "user")
    result = h.loop.handle_turn("Keep going.")
    assert result.tasks[0].state == "blocked"
    assert result.tasks[0].block_reason == "limit"
    assert "iteration limit" in result.messages[0]


def test_memory_implementation_can_be_swapped_by_setting(make_harness) -> None:
    h = make_harness([], settings={"implementations": {"memory": "inmemory"}})
    assert isinstance(h.memory, InMemoryMemoryManager)


def test_interaction_log_is_append_only_and_merges_on_read(make_harness) -> None:
    h = make_harness([plan("hello"), respond("Hello."), plan("again"), respond("Again.")])
    first = h.loop.handle_turn("hi")
    second = h.loop.handle_turn("hello again")
    rows = read_jsonl(h.data_dir / "sessions" / "interactions.jsonl")
    assert [row["kind"] for row in rows] == ["user", "assistant_update", "user", "assistant_update"]
    assert rows[1]["id"] == first.interaction_id
    assert rows[3]["id"] == second.interaction_id

    interactions = h.sessions.list_interactions()
    assert [item.user_text for item in interactions] == ["hi", "hello again"]
    assert [item.assistant_text for item in interactions] == ["Hello.", "Again."]
    assert interactions[0].task_ids


def test_settings_and_traces_record_live_turn(make_harness) -> None:
    h = make_harness([plan("status"), act("mock", "status", target="nas"), respond("Online.")])
    result = h.loop.handle_turn("Check nas status")
    traces = h.traces.list_for(interaction_id=result.interaction_id, limit=10)
    planner = [item for item in traces if item.role == "planner"]
    task_traces = [item for item in traces if item.task_id]
    assert len(planner) == 1 and planner[0].task_id == ""
    assert task_traces[0].task_id == result.tasks[0].id
    trace = task_traces[0]
    assert trace.finish_reason is None
    assert trace.token_counts["prompt"] > 0
    events = h.events.query(operation="model.call")
    assert any(event.refs and event.refs[0] == trace.task_id for event in events)
    assert h.events.query(operation="loop.act")

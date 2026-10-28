"""A. Core runtime: responses, tool lifecycle, recovery, limits."""

from __future__ import annotations

import json

from harness import util


def _say(app, text, source="web"):
    return app.runtime.submit_user_message(text, source=source)


def test_normal_message_produces_response(make_app):
    app = make_app.build(default="Hello there.")
    result = _say(app, "hi")
    assert result.status == "completed"
    assert result.message == "Hello there."
    visible = app.messages.list_visible()
    assert [m["role"] for m in visible] == ["user", "assistant"]
    assert visible[0]["source"] == "web"
    task = app.tasks.get(result.task_id)
    assert task["status"] == "completed"
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    assert run["status"] == "completed"
    assert util.loads(run["context_sources"])  # provenance recorded


def test_tool_call_validated_executed_observed_recorded(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "The rack is 42U high.", "kind": "fact"}}]},
        {"content": "Saved the rack height."},
    ])
    result = _say(app, "Remember that the rack is 42U high.")
    assert result.status == "completed"
    executions = app.db.query("SELECT * FROM tool_executions")
    assert executions[0]["tool"] == "remember"
    assert executions[0]["status"] == "succeeded"
    memories = app.memory.list()
    assert memories and "42U" in memories[0]["content"]
    # The observation reached the transcript the model saw on the next call.
    last = app.model.calls[-1]["messages"]
    assert any(m.get("role") == "tool" and "42U" in str(m) for m in last)


def test_sequential_tool_calls(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "search_knowledge",
                         "arguments": {"query": "rack height"}}]},
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "Rack height is 42U."}}]},
        {"content": "Done both steps."},
    ])
    result = _say(app, "Check what we know about the rack, then remember it.")
    assert result.status == "completed"
    assert app.db.query_one("SELECT COUNT(*) c FROM tool_executions")["c"] == 2


def test_model_recovers_from_tool_failure_without_losing_state(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "retract_memory",
                         "arguments": {"memory_id": "mem_missing",
                                       "reason": "cleanup"}}]},
        {"tool_calls": [{"name": "remember",
                         "arguments": {"content": "Rack is 42U."}}]},
        {"content": "The first call failed but I saved the fact anyway."},
    ])
    result = _say(app, "Clean up the memory then save the rack fact.")
    assert result.status == "completed"
    rows = app.db.query("SELECT tool, status FROM tool_executions ORDER BY rowid")
    assert rows[0]["status"] == "failed"
    assert rows[1]["status"] == "succeeded"
    # The failure observation was visible to the model in the transcript.
    failure_seen = json.dumps(app.model.calls[1]["messages"])
    assert "no such memory" in failure_seen


def test_unknown_tool_fails_safely(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "launch_missiles", "arguments": {}}]},
        {"content": "That tool does not exist; nothing was done."},
    ])
    result = _say(app, "Do something impossible.")
    assert result.status == "completed"
    row = app.db.query_one("SELECT * FROM tool_executions")
    assert row["status"] == "denied"
    assert "unknown tool" in (row["error"] or "")
    observation = json.dumps(app.model.calls[1]["messages"])
    assert "unknown tool" in observation


def test_invalid_arguments_reported_and_continuable(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "", "bogus": 1}}]},  # empty + unknown arg
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "Rack is 42U."}}]},
        {"content": "Fixed and saved."},
    ])
    result = _say(app, "Save the rack height.")
    assert result.status == "completed"
    rows = app.db.query("SELECT status, error FROM tool_executions ORDER BY rowid")
    assert rows[0]["status"] == "denied"
    assert "must not be empty" in rows[0]["error"]
    assert rows[1]["status"] == "succeeded"


def test_malformed_model_output_is_bounded(make_app):
    app = make_app.build(scripts=[
        {"malformed": True}, {"malformed": True}, {"malformed": True},
    ])
    result = _say(app, "hello")
    assert result.status == "failed"
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    assert run["status"] == "failed"
    task = app.tasks.get(result.task_id)
    assert task["status"] == "paused"
    assert "malformed" in (task["status_reason"] or "")


def test_model_unavailable_pauses_recoverably(make_app):
    app = make_app.build(scripts=[{"error": "connection refused"}])
    result = _say(app, "hello")
    assert result.status == "failed"
    task = app.tasks.get(result.task_id)
    assert task["status"] == "paused"
    assert task["status_reason"].startswith("model failure")


def test_iteration_budget_stops_runaway_loop(make_app):
    # The model proposes a tool call forever; the budget must stop it.
    app = make_app.build(default={"tool_calls": [
        {"name": "search_knowledge", "arguments": {"query": "loop"}}
    ]})
    app.config.set("limits.max_iterations", 4)
    app.config.set("limits.max_tool_calls", 100)
    result = _say(app, "loop forever")
    assert result.status == "limit_reached"
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    assert run["status"] == "limit_reached"
    assert "iteration limit" in run["decision"]
    task = app.tasks.get(result.task_id)
    assert task["status"] == "paused"
    # A visible, honest note explains the pause.
    notes = [m for m in app.messages.list_visible() if m["role"] == "note"]
    assert notes and "Paused" in notes[-1]["content"]


def test_tool_call_budget_stops_runaway_loop(make_app):
    app = make_app.build(default={"tool_calls": [
        {"name": "search_knowledge", "arguments": {"query": "loop"}}
    ]})
    app.config.set("limits.max_tool_calls", 3)
    app.config.set("limits.max_iterations", 100)
    result = _say(app, "loop forever")
    assert result.status == "limit_reached"
    assert "tool-call limit" in result.detail


def test_waiting_user_marker_blocks_and_resumes(make_app):
    app = make_app.build(scripts=[])
    ASK = "<" + "ask_user" + "/>"
    app.model.scripts.append({"content": f"Which rack? {ASK}"})
    result = _say(app, "check the rack")
    assert result.status == "waiting_user"
    task = app.tasks.get(result.task_id)
    assert task["status"] == "waiting_user"
    visible = app.messages.list_visible()
    assert ASK not in visible[-1]["content"]  # marker stripped

    app.model.scripts.append({"content": "Got it: the network rack is 42U."})
    answer = _say(app, "the network rack", source="cli")
    assert answer.status == "completed"
    # The answer continued the SAME task and run.
    assert answer.task_id == result.task_id
    assert answer.run_id == result.run_id
    task = app.tasks.get(answer.task_id)
    assert task["status"] == "completed"


def test_task_notes_tool_persisted(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "update_task_notes", "arguments": {
            "notes": "step 1 done; next: verify rack height"}}]},
        {"content": "Notes saved."},
    ])
    result = _say(app, "do the rack work")
    meta = app.tasks.meta(result.task_id)
    assert "step 1 done" in meta["notes"]


def test_run_transcript_persisted_each_step(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "remember", "arguments": {"content": "x=1"}}]},
        {"content": "done"},
    ])
    result = _say(app, "save x=1")
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    transcript = util.loads(run["transcript"])
    roles = [m["role"] for m in transcript]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]

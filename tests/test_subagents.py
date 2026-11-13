"""Phase 4: sequential, bounded sub-agents.

The child is a normal task/run, so every guarantee of the core loop still
applies. These tests pin the delegation contract: result return, isolation,
reduced toolset, blocked outcomes, budgets, dedupe, recovery, and the API/UI
tree.
"""

from __future__ import annotations

import json

from harness import util
from harness.db import SCHEMA_VERSION, Database


def _say(app, text, source="web"):
    return app.runtime.submit_user_message(text, source=source)


def _children(app, parent_id):
    return [
        task for task in app.tasks.list_recent(limit=100)
        if task.get("parent_task_id") == parent_id
        and (task.get("role") or "user") == "subagent"
    ]


def _tool_results(app, tool="delegate_task"):
    rows = app.db.query(
        "SELECT result FROM tool_executions WHERE tool=? AND"
        " status='succeeded' ORDER BY started_at",
        (tool,),
    )
    return [json.loads(row["result"] or "{}") for row in rows]


def test_parent_delegates_child_runs_and_result_returns(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "delegate_task", "arguments": {
            "goal": "Summarise the rack note",
            "context": "The rack is 42U high.",
            "expected_output": "One sentence.",
        }}]},
        {"content": "The rack note says 42U."},
        {"content": "Delegated summary: 42U."},
    ])
    result = _say(app, "Delegate the rack summary")
    assert result.status == "completed"
    assert result.message == "Delegated summary: 42U."

    parent = app.tasks.get(result.task_id)
    children = _children(app, parent["id"])
    assert len(children) == 1
    child = children[0]
    assert child["status"] == "completed"
    assert child["role"] == "subagent"
    assert child["origin"] == "subagent"
    assert child["domain_id"] == parent["domain_id"]
    assert child["origin_ref"].startswith(parent["id"] + ":")

    child_run = app.db.query_one(
        "SELECT * FROM runs WHERE task_id=?", (child["id"],)
    )
    assert child_run["status"] == "completed"
    assert child_run["parent_run_id"] == result.run_id

    meta = app.tasks.meta(child["id"])
    assert meta["result"] == "The rack note says 42U."
    artifact = app.artifacts_dir / meta["result_path"]
    assert artifact.is_file()
    body = artifact.read_text(encoding="utf-8")
    assert "The rack note says 42U." in body
    assert "The rack is 42U high." in body

    payload = _tool_results(app)[-1]
    assert payload["status"] == "completed"
    assert payload["child_task_id"] == child["id"]
    assert payload["result"] == "The rack note says 42U."
    assert payload["artifact_path"] == meta["result_path"]

    events = {event["type"] for event in app.events.list(limit=200)}
    assert {"subagent.started", "subagent.completed"} <= events
    # Child messages are inspectable but never leak into ordinary chat.
    visible = [m["content"] for m in app.messages.list_visible()]
    assert "The rack note says 42U." not in visible
    child_messages = app.messages.for_task(child["id"])
    assert child_messages and all(not m["visible"] for m in child_messages)


def test_child_context_omits_parent_transcript(make_app):
    app = make_app.build(scripts=[
        {"content": "Noted."},
        {"tool_calls": [{"name": "delegate_task", "arguments": {
            "goal": "Summarise the rack notes",
        }}]},
        {"content": "Rack summary for the child."},
        {"content": "Parent final."},
    ])
    _say(app, "The vault code is SECRET-123.")
    result = _say(app, "Please delegate a summary of the rack notes.")
    parent = app.tasks.get(result.task_id)
    children = _children(app, parent["id"])
    assert len(children) == 1
    transcript = util.loads(app.db.query_one(
        "SELECT transcript FROM runs WHERE task_id=?", (children[0]["id"],)
    )["transcript"])
    system = transcript[0]["content"]
    assert "Summarise the rack notes" in system
    assert "SECRET-123" not in system
    assert "Earlier work on this task" not in system
    assert "Recent conversation" not in system
    assert "Sub-agent rules" in system


def test_child_cannot_delegate_or_use_confirm_tools(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "delegate_task", "arguments": {
            "goal": "Try to delegate again"}}]},
        {"tool_calls": [{"name": "delegate_task", "arguments": {
            "goal": "A deeper child"}}]},
        {"tool_calls": [{"name": "delegate_task", "arguments": {
            "goal": "Try a confirm action"}}]},
        {"tool_calls": [{"name": "propose_skill", "arguments": {
            "name": "rack-procedure", "description": "rack work",
            "body": "Steps."}}]},
        {"content": "Parent saw both blocked."},
    ])
    result = _say(app, "Delegate two restricted attempts")
    assert result.status == "completed"
    parent = app.tasks.get(result.task_id)
    children = _children(app, parent["id"])
    assert len(children) == 2
    for child in children:
        snapshot = util.loads(app.db.query_one(
            "SELECT meta FROM runs WHERE task_id=?", (child["id"],)
        )["meta"])["toolset"]
        assert "delegate_task" not in snapshot["names"]
        assert "create_domain" not in snapshot["names"]
        assert "propose_skill" not in snapshot["names"]
        for name in snapshot["names"]:
            assert app.tools.resolve_optional(name).risk == "low"
            assert name not in ("delegate_task", "create_domain")

    payloads = _tool_results(app)
    assert [p["status"] for p in payloads] == ["blocked", "blocked"]
    assert "unavailable to sub-agents" in payloads[0]["reason"]
    assert "confirmation-gated" in payloads[1]["reason"]
    # No approval was created for the child's confirm attempt.
    assert app.approvals.pending() == []


def test_child_limit_returns_blocked_and_parent_continues(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "delegate_task", "arguments": {
            "goal": "Keep searching forever"}}]},
        {"tool_calls": [{"name": "search_knowledge", "arguments": {
            "query": "loop"}}]},
        {"content": "The child was stopped; continuing without it."},
    ])
    app.config.set("subagents.max_tool_calls", 1)
    result = _say(app, "Delegate a runaway child")
    assert result.status == "completed"
    assert result.message == "The child was stopped; continuing without it."
    parent = app.tasks.get(result.task_id)
    child = _children(app, parent["id"])[0]
    assert child["status"] == "cancelled"
    assert "budget limit" in (app.tasks.meta(child["id"])["blocked_reason"])
    payload = _tool_results(app)[-1]
    assert payload["status"] == "blocked"
    assert "tool-call limit" in payload["reason"]


def test_same_goal_reuses_the_existing_child(make_app):
    goal = "Reuse this exact goal"
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "delegate_task",
                         "arguments": {"goal": goal}}]},
        {"content": "Only one child ever runs."},
        {"tool_calls": [{"name": "delegate_task",
                         "arguments": {"goal": goal}}]},
        {"content": "Both delegations are done."},
    ])
    result = _say(app, "Delegate the same goal twice")
    assert result.status == "completed"
    parent = app.tasks.get(result.task_id)
    assert len(_children(app, parent["id"])) == 1
    payloads = _tool_results(app)
    assert len(payloads) == 2
    assert payloads[0]["reused"] is False
    assert payloads[1]["reused"] is True
    assert payloads[1]["child_task_id"] == payloads[0]["child_task_id"]
    assert any(
        event["type"] == "subagent.reused" for event in app.events.list(200)
    )


def test_child_budget_caps_and_task_metadata(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "delegate_task", "arguments": {
            "goal": "First child"}}]},
        {"content": "First result."},
        {"tool_calls": [{"name": "delegate_task", "arguments": {
            "goal": "Second child"}}]},
        {"content": "Second result."},
        {"content": "Parent stopped at its child cap."},
    ])
    app.config.set("subagents.max_children_per_run", 1)
    result = _say(app, "Delegate two distinct children")
    assert result.status == "completed"
    parent = app.tasks.get(result.task_id)
    children = _children(app, parent["id"])
    assert len(children) == 1
    budget = app.tasks.meta(children[0]["id"])["budget"]
    assert budget == {
        "max_iterations": 8,
        "max_tool_calls": 12,
        "max_seconds": 180.0,
    }
    payloads = _tool_results(app)
    assert payloads[0]["status"] == "completed"
    assert payloads[1]["status"] == "blocked"
    assert "child budget" in payloads[1]["reason"]


def test_crash_mid_child_is_unknown_for_parent(make_app):
    app = make_app.build(scripts=[{"content": "unused"}])
    parent = app.tasks.create(objective="parent work", origin="user")
    parent_run_id = util.new_id("run")
    call_id = "call_delegate_crash"
    transcript = [
        {"role": "system", "content": "ctx"},
        {"role": "user", "content": "parent work"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "delegate_task", "arguments": json.dumps(
                {"goal": "child work"})},
        }]},
    ]
    app.db.execute(
        "INSERT INTO runs(id, task_id, trigger, status, started_at,"
        " iterations, tool_calls, transcript) VALUES (?,?,?,?,?,?,?,?)",
        (parent_run_id, parent["id"], "user", "running", util.iso(), 1, 0,
         util.dumps(transcript)),
    )
    child = app.tasks.create(
        objective="child work", origin="subagent",
        origin_ref=f"{parent['id']}:crashed",
        parent_task_id=parent["id"], parent_run_id=parent_run_id,
        role="subagent", meta={"depth": 1, "budget": {}},
    )
    app.db.execute(
        "INSERT INTO runs(id, task_id, trigger, status, started_at,"
        " parent_run_id) VALUES (?,?,?,?,?,?)",
        (util.new_id("run"), child["id"], "subagent", "running",
         util.iso(), parent_run_id),
    )
    app.db.execute(
        "INSERT INTO tool_executions(id, run_id, task_id, tool, tool_call_id,"
        " arguments, status, started_at) VALUES (?,?,?,?,?,?,?,?)",
        (util.new_id("ex"), parent_run_id, parent["id"], "delegate_task",
         call_id, json.dumps({"goal": "child work"}), "in_flight",
         util.iso()),
    )
    app.tasks.set_status(parent["id"], "active", reason="mid-child")

    app2 = make_app.restart(scripts=[
        {"content": "I saw the unknown delegation; not repeating it."},
    ])
    execution = app2.db.query_one(
        "SELECT * FROM tool_executions WHERE tool='delegate_task'"
        " AND task_id=?", (parent["id"],)
    )
    assert execution["status"] == "unknown"
    assert "unknown" in (execution["error"] or "")
    parent_run = app2.db.query_one(
        "SELECT * FROM runs WHERE id=?", (parent_run_id,)
    )
    assert parent_run["status"] == "interrupted"
    assert "unknown" in parent_run["transcript"]
    assert app2.tasks.get(child["id"])["status"] == "paused"
    child_run = app2.db.query_one(
        "SELECT status FROM runs WHERE task_id=?", (child["id"],)
    )
    assert child_run["status"] == "interrupted"

    result = app2.runtime.retry_paused_task(app2.tasks.get(parent["id"]))
    assert result.status == "completed"
    assert app2.tasks.get(parent["id"])["status"] == "completed"
    # Recovery never duplicated the child or silently re-ran the delegation.
    assert len(_children(app2, parent["id"])) == 1
    count = app2.db.query_one(
        "SELECT COUNT(*) c FROM tool_executions WHERE tool='delegate_task'"
        " AND task_id=?", (parent["id"],)
    )["c"]
    assert count == 1


def test_subagent_schema_v5_migration_adds_parent_columns(tmp_path):
    path = tmp_path / "harness.db"
    db = Database(path)
    for index in ("idx_tasks_parent", "idx_runs_parent"):
        db.execute(f"DROP INDEX {index}")
    db.execute("ALTER TABLE tasks DROP COLUMN role")
    db.execute("ALTER TABLE tasks DROP COLUMN parent_task_id")
    db.execute("ALTER TABLE tasks DROP COLUMN parent_run_id")
    db.execute("ALTER TABLE runs DROP COLUMN parent_run_id")
    db.execute("UPDATE schema_meta SET value='4' WHERE key='schema_version'")
    db.close()

    reopened = Database(path)
    try:
        version = reopened.query_one(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        )["value"]
        assert int(version) == SCHEMA_VERSION == 5
        task_columns = {
            row["name"] for row in reopened.query("PRAGMA table_info(tasks)")
        }
        assert {"parent_task_id", "parent_run_id", "role"} <= task_columns
        run_columns = {
            row["name"] for row in reopened.query("PRAGMA table_info(runs)")
        }
        assert "parent_run_id" in run_columns
    finally:
        reopened.close()


def test_task_detail_returns_child_tree_and_hides_children_from_state(
    make_app, client
):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "delegate_task", "arguments": {
            "goal": "Child for the detail view"}}]},
        {"content": "Child detail result."},
        {"content": "Parent complete."},
    ])
    result = _say(app, "Delegate for the detail view")
    http = client(app)
    detail = http.get(f"/api/tasks/{result.task_id}").json()
    assert len(detail["children"]) == 1
    child = detail["children"][0]
    assert child["role"] == "subagent"
    assert child["parent_task_id"] == result.task_id
    assert child["result"] == "Child detail result."
    state = http.get("/api/state").json()
    assert all(
        (task.get("role") or "user") != "subagent"
        for task in state["tasks"]
    )

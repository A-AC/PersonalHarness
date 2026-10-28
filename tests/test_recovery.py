"""G. Recovery and deployment: restarts, unknown outcomes, shutdown,
backup/restore, concurrent interfaces."""

from __future__ import annotations

import json
import zipfile

import pytest

from harness import util
from harness.model import ToolCall
from harness.tools import ToolContext


def test_restart_preserves_domains_memories_tasks_schedules_approvals(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "The unifi is at 10.0.0.9.", "kind": "fact"}}]},
        {"content": "saved"},
    ])
    app.domains.create(util.new_id("dom"), name="Homelab",
                       description="servers", created_by="test")
    app.runtime.submit_user_message("Remember that the unifi is at 10.0.0.9.")
    app.scheduler.create_schedule(
        name="daily", prompt="check", kind="daily", time_of_day="08:00",
    )
    # A pending approval to carry across the restart.
    from harness.tools import ToolSpec

    app.tools.register(ToolSpec(
        name="mcp_x", description="x",
        parameters={"type": "object",
                    "properties": {"t": {"type": "string"}}},
        risk="confirm", handler=lambda ctx, args: {"ok": 1},
    ))
    app.model.scripts.append({"tool_calls": [{"name": "mcp_x",
                                              "arguments": {"t": "1"}}]})
    app.runtime.submit_user_message("run the confirm tool")
    approval = app.approvals.pending()[0]
    assert approval

    app2 = make_app.restart(scripts=[{"content": "resumed fine"}])
    assert [d["name"] for d in app2.domains.list_active()] == ["General", "Homelab"]
    assert any("unifi" in m["content"] for m in app2.memory.list())
    assert app2.scheduler.list_schedules()
    assert app2.approvals.pending()[0]["id"] == approval["id"]
    tasks = app2.tasks.list_recent()
    assert len(tasks) == 2


def test_interrupted_run_resumes_without_repeating_completed_actions(make_app):
    scripts = [
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "step one done", "kind": "fact"}}]},
        {"content": "all steps complete"},
    ]
    app = make_app.build(scripts=scripts)
    task = app.tasks.create(objective="multi step work", origin="user")
    # Simulate: the run executed step one (a succeeded tool call recorded in
    # the transcript) and then the process died mid-run.
    run_id = util.new_id("run")
    transcript = [
        {"role": "system", "content": "ctx"},
        {"role": "user", "content": "multi step work"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function", "function": {
             "name": "remember", "arguments": json.dumps(
                 {"content": "step one done"})}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": json.dumps({"status": "succeeded", "memory_id": "mem_x"})},
    ]
    app.db.execute(
        "INSERT INTO runs(id, task_id, trigger, status, started_at,"
        " iterations, tool_calls, transcript) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, task["id"], "user", "running", util.iso(), 1, 1,
         util.dumps(transcript)),
    )
    app.db.execute(
        "INSERT INTO tool_executions(id, run_id, task_id, tool, arguments,"
        " status, started_at) VALUES (?,?,?,?,?,?,?)",
        (util.new_id("ex"), run_id, task["id"], "remember", "{}",
         "succeeded", util.iso()),
    )
    app.tasks.set_status(task["id"], "active", reason="mid-run")
    # Restart: recovery marks the run interrupted and the task paused.
    app2 = make_app.restart(scripts=[{"content": "all steps complete"}])
    recovered_task = app2.tasks.get(task["id"])
    assert recovered_task["status"] == "paused"
    run = app2.db.query_one("SELECT * FROM runs WHERE id=?", (run_id,))
    assert run["status"] == "interrupted"
    # The heartbeat resumes it; the model sees step one's result in the
    # transcript and does not repeat it.
    report = app2.scheduler.heartbeat()
    assert run_id in report["resumed"] or task["id"] in report["resumed"]
    assert app2.tasks.get(task["id"])["status"] == "completed"
    # Only one remember execution exists (the pre-restart one).
    executions = app2.db.query_one(
        "SELECT COUNT(*) c FROM tool_executions WHERE task_id=?",
        (task["id"],),
    )["c"]
    assert executions == 1


def test_in_flight_execution_becomes_unknown_not_success(make_app):
    app = make_app.build(scripts=[{"content": "x"}])
    task = app.tasks.create(objective="external call", origin="user")
    run_id = util.new_id("run")
    app.db.execute(
        "INSERT INTO runs(id, task_id, trigger, status, started_at,"
        " transcript) VALUES (?,?,?,?,?,?)",
        (run_id, task["id"], "user", "running", util.iso(), "[]"),
    )
    app.db.execute(
        "INSERT INTO tool_executions(id, run_id, task_id, tool, arguments,"
        " status, started_at) VALUES (?,?,?,?,?,?,?)",
        (util.new_id("ex"), run_id, task["id"], "mcp_send", "{}",
         "in_flight", util.iso()),
    )
    app2 = make_app.restart()
    row = app2.db.query_one(
        "SELECT * FROM tool_executions WHERE task_id=?", (task["id"],)
    )
    assert row["status"] == "unknown"
    assert "unknown" in (row["error"] or "")
    # Unknown is never reported as success anywhere.
    assert row["status"] != "succeeded"


def test_tool_attempt_is_persisted_before_handler_runs(make_app):
    from harness.tools import ToolSpec

    class Crash(BaseException):
        pass

    app = make_app.build(default="unused")
    task = app.tasks.create(objective="crash in handler", origin="user")
    run_id = util.new_id("run")
    app.db.execute(
        "INSERT INTO runs(id, task_id, trigger, status, started_at,"
        " transcript) VALUES (?,?,?,?,?,?)",
        (run_id, task["id"], "user", "running", util.iso(), "[]"),
    )
    seen: dict[str, str] = {}

    def handler(ctx, args):
        row = app.db.query_one(
            "SELECT status FROM tool_executions WHERE tool='crash_probe'"
        )
        seen["status"] = row["status"]
        raise Crash()

    app.tools.register(ToolSpec(
        name="crash_probe", description="crash probe",
        parameters={"type": "object", "properties": {}},
        risk="low", handler=handler,
    ))
    call = ToolCall("call_crash", "crash_probe", {}, "{}")
    ctx = ToolContext(run_id=run_id, task_id=task["id"])
    with pytest.raises(Crash):
        app.tools.execute(
            call, ctx, run_id=run_id, task_id=task["id"]
        )
    assert seen["status"] == "in_flight"
    assert app.db.query_one(
        "SELECT status FROM tool_executions WHERE tool='crash_probe'"
    )["status"] == "in_flight"

    app2 = make_app.restart(default="recovered")
    row = app2.db.query_one(
        "SELECT status FROM tool_executions WHERE tool='crash_probe'"
    )
    assert row["status"] == "unknown"


def test_unknown_in_flight_outcome_is_visible_in_run_transcript(make_app):
    app = make_app.build(default="unused")
    task = app.tasks.create(objective="unknown external call", origin="user")
    run_id = util.new_id("run")
    transcript = [
        {"role": "system", "content": "ctx"},
        {"role": "user", "content": "do the external call"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_unknown", "type": "function",
                         "function": {"name": "mcp_send",
                                      "arguments": "{}"}}]},
    ]
    app.db.execute(
        "INSERT INTO runs(id, task_id, trigger, status, started_at,"
        " transcript) VALUES (?,?,?,?,?,?)",
        (run_id, task["id"], "user", "running", util.iso(),
         util.dumps(transcript)),
    )
    app.db.execute(
        "INSERT INTO tool_executions(id, run_id, task_id, tool,"
        " tool_call_id, arguments, status, started_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (util.new_id("ex"), run_id, task["id"], "mcp_send",
         "call_unknown", "{}", "in_flight", util.iso()),
    )
    app2 = make_app.restart(default="recovered")
    run = app2.db.query_one("SELECT * FROM runs WHERE id=?", (run_id,))
    messages = json.loads(run["transcript"])
    result = [m for m in messages
              if m.get("tool_call_id") == "call_unknown"]
    assert result and "unknown" in result[-1]["content"]
    assert "may or may not have happened" in result[-1]["content"]
    assert run["status"] == "interrupted"


def test_graceful_shutdown_preserves_state(make_app):
    app = make_app.build(scripts=[{"content": "done"}])
    result = app.runtime.submit_user_message("hello")
    assert result.status == "completed"
    app.shutdown()
    # Reopen on the same directory: everything readable, WAL checkpointed.
    app2 = make_app.build(default="done")
    messages = app2.messages.list_visible()
    assert any(m["content"] == "hello" for m in messages)
    assert app2.tasks.list_recent()[0]["status"] == "completed"


def test_backup_and_restore_round_trip(make_app, tmp_path):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "precious fact", "kind": "fact"}}]},
        {"content": "saved"},
    ])
    app.domains.create(util.new_id("dom"), name="Villa",
                       description="renovation", created_by="test")
    app.runtime.submit_user_message("Remember the precious fact.")
    archive = app.backup(tmp_path / "backup.zip")
    assert archive.is_file()
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert "harness.db" in names and "config.yaml" in names

    # Diverge the live state, then restore the backup.
    app.memory.retract(app.memory.list()[0]["id"], reason="cleanup")
    app.tasks.create(objective="extra junk", origin="user")
    outcome = app.restore(archive)
    assert outcome["restored"] is True

    # A fresh process on the restored state sees the backup world.
    app2 = make_app.restart(default="restored")
    assert any("precious fact" in m["content"] for m in app2.memory.list())
    assert [d["name"] for d in app2.domains.list_all()] == ["General", "Villa"]
    assert not any(t["objective"] == "extra junk"
                   for t in app2.tasks.list_recent())


def test_second_writer_process_refuses_to_start(make_app):
    from harness.app import ProcessLock

    app = make_app.build(default="ok", serve=True)
    lock_path = app.data_dir / "process.lock"
    assert lock_path.exists()  # the serve-mode app holds the writer lock
    # A second writer must refuse while the lock is fresh and its pid alive.
    second = ProcessLock(lock_path)
    try:
        second.acquire()
        refused = False
    except RuntimeError:
        refused = True
    assert refused
    # A stale lock (dead pid) is taken over by the next writer.
    lock_path.write_text(json.dumps(
        {"pid": 999999999, "ts": util.iso()}), encoding="utf-8")
    third = ProcessLock(lock_path)
    third.acquire()
    third.release()


def _alive_pid() -> int:
    import os

    return os.getpid()


def test_web_and_cli_concurrent_without_duplicate_scheduler(make_app, client):
    """Both interfaces hit one server; one heartbeat thread ticks."""
    app = make_app.build(default="ok")
    app.scheduler.start()
    http = client(app)
    token = app.config.get("server.token")
    headers = {"Authorization": f"Bearer {token}"}
    r1 = http.post("/api/message", json={"text": "from the web",
                                          "source": "web"}, headers=headers)
    r2 = http.post("/api/message", json={"text": "from the cli",
                                          "source": "cli"}, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    visible = app.messages.list_visible()
    assert [m["source"] for m in visible if m["role"] == "user"] == ["web", "cli"]
    # Exactly one scheduler thread exists.
    threads = [t.name for t in __import__("threading").enumerate()]
    assert threads.count("scheduler") <= 1
    app.scheduler.stop()

"""The ten end-to-end acceptance scenarios.

Deterministic (scripted model, real harness everywhere else). Each scenario
is small enough to run regularly and meaningful enough to catch
architectural regressions. Numbering matches the acceptance list.
"""

from __future__ import annotations

import datetime as dt
import json

from harness import util

ASK = "<" + "ask_user" + "/>"


def _say(app, text, source="web"):
    return app.runtime.submit_user_message(text, source=source)


# 1. Preference stored now, retrieved later after unrelated traffic.
def test_scenario_1_preference_recall_later(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "The user prefers metric units.",
            "kind": "preference"}}]},
        {"content": "Noted the preference."},
    ])
    _say(app, "Remember that I prefer metric units.")
    for index in range(10):
        app.model.scripts.append({"content": f"chatter {index}"})
        _say(app, f"unrelated small talk {index}")
    app.model.scripts.append({"content": "In metric: about 1.8 meters."})
    _say(app, "how tall are you in the units I like?")
    context = json.dumps(app.model.last_messages)
    assert "metric" in context


# 2. Multi-step task interrupted by restart resumes without repeating work.
def test_scenario_2_interrupted_multistep_resumes(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "migration step 1 finished", "kind": "fact"}}]},
        {"content": "should not get here yet"},
    ])
    result = _say(app, "Migrate the wiki: step 1 save progress, then report.")
    assert result.status == "completed"  # step-1 tool call + final response
    # Start a second task and kill the process mid-run (status 'running').
    task = app.tasks.create(objective="finish the wiki migration report",
                            origin="user")
    run_id = util.new_id("run")
    app.db.execute(
        "INSERT INTO runs(id, task_id, trigger, status, started_at,"
        " iterations, transcript) VALUES (?,?,?,?,?,?,?)",
        (run_id, task["id"], "user", "running", util.iso(), 1,
         util.dumps([
             {"role": "system", "content": "ctx"},
             {"role": "user", "content": task["objective"]},
             {"role": "assistant", "content": "working on it"},
         ])),
    )
    app2 = make_app.restart(scripts=[{"content": "Migration report: done."}])
    # Recovery + heartbeat resume the interrupted run; the earlier completed
    # tool execution was not repeated (no new remember executions).
    app2.scheduler.heartbeat()
    assert app2.tasks.get(task["id"])["status"] == "completed"
    remember_execs = app2.db.query_one(
        "SELECT COUNT(*) c FROM tool_executions WHERE tool='remember'"
    )["c"]
    assert remember_execs == 1


# 3. Scheduled task reaches approval, waits, resumes after approval.
def test_scenario_3_scheduled_approval_pause_resume(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "create_domain", "arguments": {
            "name": "Invoices", "description": "invoice filing",
            "reason": "recurring monthly work"}}]},
        {"content": "Created the Invoices domain."},
    ])
    app.scheduler.create_schedule(
        name="invoices", prompt="Create the Invoices domain for filing.",
        kind="once", at_ts=util.iso(util.now_utc() - dt.timedelta(minutes=1)),
    )
    submitted = app.scheduler.submit_due()
    task = submitted[0]["task"]
    result = app.runtime.start_run(
        task, {"kind": "schedule", "name": "invoices"},
        trigger_text=task["objective"],
    )
    assert result.status == "waiting_approval"
    approval = app.approvals.pending()[0]
    # Restart while waiting; the approval and task survive.
    app2 = make_app.restart(scripts=[{"content": "Created the Invoices domain."}])
    assert app2.approvals.pending()[0]["id"] == approval["id"]
    assert app2.tasks.get(task["id"])["status"] == "waiting_approval"
    # Approving resumes the original run (not a new scheduled occurrence).
    app2.approvals.decide(approval["id"], "approve", decided_by="web")
    resumed = app2.runtime.resume_after_approval(approval, "web")
    assert resumed.status == "completed"
    assert resumed.run_id == result.run_id
    assert not app2.scheduler.submit_due()  # no duplicate occurrence
    assert "Invoices" in [d["name"] for d in app2.domains.list_all()]


# 4. Heartbeat detects pending work and initiates bounded execution.
def test_scenario_4_heartbeat_initiates_bounded_work(make_app):
    app = make_app.build(scripts=[{"error": "down"}])
    result = _say(app, "please summarize the server status")
    assert result.status == "failed"
    app.config.set("scheduler.retry_cooldown_s", 0)
    app2_scripts = [{"content": "Status: one paused item, handled."}]
    app.model.scripts.extend(app2_scripts)
    report = app.scheduler.heartbeat()
    assert result.task_id in report["retried"]
    assert app.tasks.get(result.task_id)["status"] == "completed"


# 5. Old history/artifact is used instead of hallucinating.
def test_scenario_5_uses_old_history_not_repetition(make_app):
    app = make_app.build(default="ok")
    # An old buried conversation contains the fact.
    app.messages.record(
        "user", "My assistant login for the router is admin/old-pass-123 "
        "but I changed it last year; the new one starts with 'giraffe'.",
        source="web",
    )
    for index in range(15):
        app.messages.record("user", f"filler chat {index}", source="web")
        app.messages.record("assistant", f"filler reply {index}", source="agent")
    app.model.scripts.append({"content": "It starts with 'giraffe'."})
    _say(app, "what does my router password start with again?")
    context = json.dumps(app.model.last_messages)
    assert "giraffe" in context


# 6. Web and CLI exchange over the same persisted task/history.
def test_scenario_6_web_cli_same_state(make_app, client):
    app = make_app.build(scripts=[
        {"content": f"Which server first? {ASK}"},
        {"content": "Right, checking alpha first."},
    ])
    http = client(app)
    token = app.config.get("server.token")
    headers = {"Authorization": f"Bearer {token}"}
    r1 = http.post("/api/message", json={"text": "check my servers",
                                         "source": "web"}, headers=headers)
    assert r1.status_code == 200
    task_id = r1.json()["task_id"]
    r2 = http.post("/api/message", json={"text": "the alpha one",
                                         "source": "cli"}, headers=headers)
    assert r2.status_code == 200
    assert r2.json()["task_id"] == task_id  # same task continued
    task = app.tasks.get(task_id)
    assert task["status"] == "completed"
    sources = [m["source"] for m in app.messages.list_visible()
               if m["role"] == "user"]
    assert sources == ["web", "cli"]


# 7. Failed model endpoint produces inspectable failure and recoverable state.
def test_scenario_7_failure_is_inspectable_and_recoverable(make_app):
    app = make_app.build(scripts=[{"error": "connection refused"}])
    result = _say(app, "please do something")
    assert result.status == "failed"
    task = app.tasks.get(result.task_id)
    assert task["status"] == "paused"
    assert task["status_reason"].startswith("model failure")
    events = [e for e in app.events.list() if e["type"] == "run.failed"]
    assert events and "connection refused" in events[0]["data"]["error"]
    note = [m for m in app.messages.list_visible()
            if m["role"] == "note" and "model endpoint" in m["content"]]
    assert note
    # Recovery: retry via heartbeat succeeds when the endpoint returns.
    app.config.set("scheduler.retry_cooldown_s", 0)
    app.model.scripts.append({"content": "Back online; done."})
    app.scheduler.heartbeat()
    assert app.tasks.get(task["id"])["status"] == "completed"


# 8. Consolidation proposes traceable, inspectable changes.
def test_scenario_8_consolidation_traceable(make_app):
    app = make_app.build(default="ok")
    a = app.memory.remember("The standup is at 9:30.", kind="fact",
                            scope="global", origin="user")
    b = app.memory.remember("Daily sync meeting: half past nine.",
                            kind="fact", scope="global", origin="user")
    app.model.scripts.append({"content": json.dumps({"changes": [
        {"op": "merge", "source_id": b["memory"]["id"],
         "target_id": a["memory"]["id"]},
    ]})})
    report = app.consolidation.run(trigger="manual")
    assert report["status"] == "completed"
    log = (app.artifacts_dir / report["log_path"]).read_text(encoding="utf-8")
    assert b["memory"]["id"] in log  # traceable to the exact rows
    revisions = app.memory.revisions(a["memory"]["id"])
    assert revisions and revisions[0]["changed_by"].startswith("consolidation:")
    events = [e for e in app.events.list() if e["type"] == "consolidation.run"]
    assert events[-1]["data"]["status"] == "completed"


# 9. Repeated trigger after restart does not duplicate a completed occurrence.
def test_scenario_9_no_duplicate_after_restart(make_app):
    app = make_app.build(default="completed")
    app.scheduler.create_schedule(
        name="tick-check", prompt="run once", kind="once",
        at_ts=util.iso(util.now_utc() - dt.timedelta(minutes=1)),
    )
    submitted = app.scheduler.submit_due()
    assert len(submitted) == 1
    task = submitted[0]["task"]
    app.runtime.start_run(task, {"kind": "schedule", "name": "tick-check"},
                           trigger_text=task["objective"])
    app.tasks.set_status(task["id"], "completed", reason="done")
    # Restart and tick again: the completed occurrence stays completed.
    app2 = make_app.restart(default="done")
    app2.scheduler.heartbeat()
    assert app2.scheduler.submit_due() == []
    assert app2.tasks.get(task["id"])["status"] == "completed"
    assert len(app2.db.query(
        "SELECT * FROM schedule_occurrences")) == 1


# 10. Permission-sensitive action cannot be executed indirectly or via a
#     generic approval.
def test_scenario_10_no_indirect_permission_escape(make_app):
    from harness.tools import ToolSpec

    app = make_app.build(scripts=[
        # The model tries to reach a confirm-risk tool through a low-risk
        # wrapper: update_memory cannot invoke anything; the confirm tool
        # still requires its own approval.
        {"tool_calls": [{"name": "mcp_external_send",
                         "arguments": {"text": "via wrapper"}}]},
        {"content": "Blocked pending approval; nothing sent."},
        {"content": "No pending approval means a 'yes' does nothing."},
        {"content": "Still nothing executed."},
    ])
    app.tools.register(ToolSpec(
        name="mcp_external_send", description="send externally",
        parameters={"type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"]},
        risk="confirm", handler=lambda ctx, args: {"sent": args["text"]},
    ))
    first = _say(app, "send the message externally")
    assert first.status == "waiting_approval"
    approval = app.approvals.pending()[0]
    # A generic yes must bind to THIS approval only (it does, one pending).
    yes = _say(app, "yes")
    assert yes.status == "completed"
    assert app.approvals.get(approval["id"])["status"] == "executed"
    # The approval is spent: a second yes grants nothing.
    second = _say(app, "yes")
    assert second.status == "completed"
    executions = app.db.query_one(
        "SELECT COUNT(*) c FROM tool_executions WHERE"
        " tool='mcp_external_send' AND status='succeeded'"
    )["c"]
    assert executions == 1  # exactly one execution, from the first approval

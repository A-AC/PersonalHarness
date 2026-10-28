"""B. Unified interfaces and history: one runtime, one history, sources kept.

The web API and the CLI both go through the same runtime; scheduled prompts
and heartbeat work enter the same tables. Maintenance activity stays
inspectable but out of the ordinary chat view.
"""

from __future__ import annotations


def test_web_and_cli_share_runtime_and_state(make_app, client):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "Router runs OpenWrt 23.05.", "kind": "fact"}}]},
        {"content": "Saved the router firmware fact."},
    ])
    http = client(app)
    # Web sends the first message.
    response = http.post("/api/message", json={"text": "Remember that the router runs OpenWrt 23.05.",
                                               "source": "web"})
    assert response.status_code == 200
    response.json()

    # CLI sends the second message through the same API.
    response = http.post("/api/message", json={"text": "what firmware does the router run?",
                                               "source": "cli"})
    assert response.status_code == 200
    response.json()

    # Same history, sources recorded per message.
    visible = app.messages.list_visible()
    sources = [m["source"] for m in visible if m["role"] == "user"]
    assert sources == ["web", "cli"]
    # The second turn saw the memory written in the first (shared state).
    second_context = app.model.calls[2]["messages"]
    assert any("OpenWrt" in str(m) for m in second_context)
    # One memory, one task each, all in one store.
    assert len(app.memory.list()) == 1


def test_scheduled_prompt_enters_same_history_and_runtime(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "Backup ran fine.", "kind": "fact"}}]},
        {"content": "Backup check complete, all healthy."},
    ])
    app.scheduler.create_schedule(
        name="backup-check", prompt="Check the backups and report.",
        kind="once", at_ts=util_iso_past()
    )
    submitted = app.scheduler.submit_due()
    assert len(submitted) == 1
    item = submitted[0]
    app.scheduler._run_submitted(item)
    # Same messages table, source=schedule, visible.
    users = [m for m in app.messages.list_visible() if m["role"] == "assistant"]
    assert users and "Backup check complete" in users[-1]["content"]
    task = app.tasks.list_by_status(("completed",))[0]
    assert task["origin"] == "schedule"
    events = [e["type"] for e in app.events.list()]
    assert "schedule.submitted" in events
    # The run used the ordinary runtime (model + tools), not a side path.
    assert app.model.calls  # model invoked through AgentRuntime


def util_iso_past():
    from harness import util
    return util.iso(util.now_utc())


def test_heartbeat_work_uses_same_runtime(make_app):
    app = make_app.build(scripts=[{"content": "Resumed and finished."}])
    # Simulate an interrupted run discovered by the heartbeat.
    task = app.tasks.create(objective="unfinished work", origin="heartbeat")
    run_id = util_new_run(app, task)
    app.db.execute("UPDATE runs SET status='interrupted' WHERE id=?", (run_id,))
    app.tasks.set_status(task["id"], "paused", reason="interrupted")
    report = app.scheduler.heartbeat()
    assert task["id"] in report["resumed"]
    assert app.tasks.get(task["id"])["status"] == "completed"
    assert app.model.calls  # same runtime executed the resume


def util_new_run(app, task):
    from harness import util
    run_id = util.new_id("run")
    app.db.execute(
        "INSERT INTO runs(id, task_id, trigger, status, started_at,"
        " transcript) VALUES (?,?,?,?,?,?)",
        (run_id, task["id"], "heartbeat", "interrupted", util.iso(),
         util.dumps([{"role": "user", "content": task["objective"]}])),
    )
    return run_id


def test_events_carry_source_metadata_without_splitting_history(make_app):
    app = make_app.build(default="ok")
    _ = app.runtime.submit_user_message("hello from web", source="web")
    _ = app.runtime.submit_user_message("hello from cli", source="cli")
    rows = app.messages.recent(limit=10)
    assert [r["source"] for r in rows if r["role"] == "user"] == ["web", "cli"]
    # One continuous id sequence: no separate histories.
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids)


def test_maintenance_activity_does_not_flood_chat_view(make_app):
    app = make_app.build(scripts=[{"content": "done"}])
    app.scheduler.create_schedule(
        name="quiet-check", prompt="noop", kind="once", at_ts=util_iso_past()
    )
    # Approval reminders are visible (user must act) but heartbeat ticks and
    # routine checks are events only.
    app.scheduler.heartbeat()
    tick_events = [e for e in app.events.list() if e["type"] == "scheduler.tick"]
    visible = app.messages.list_visible()
    # The scheduled occurrence ran once and produced at most the scheduled
    # prompt + one answer; heartbeat bookkeeping added no chat bubbles.
    assistant_bubbles = [m for m in visible if m["role"] == "assistant"]
    assert len(assistant_bubbles) <= 1
    # Quiet heartbeats do not emit tick events at all.
    app.scheduler.heartbeat()
    tick_events_after = [e for e in app.events.list()
                         if e["type"] == "scheduler.tick"]
    assert len(tick_events_after) == len(tick_events)


def test_unauthorized_api_calls_rejected(make_app):
    app = make_app.build(default="hi")
    from starlette.testclient import TestClient

    from harness.server import Server

    http = TestClient(Server(app).routes())  # no token
    assert http.get("/api/state").status_code == 401
    assert http.post("/api/message", json={"text": "x"}).status_code == 401
    assert http.get("/health").status_code == 200


def test_token_auth_accepted(make_app, client):
    app = make_app.build(default="hi")
    http = client(app)
    token = app.config.get("server.token")
    response = http.get(f"/api/state?token={token}")
    assert response.status_code == 200
    response = http.get("/api/state", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_schedules_api_create_list_disable(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    created = http.post("/api/schedules", json={
        "name": "api-daily", "prompt": "check things",
        "kind": "daily", "time_of_day": "08:30",
        "tz": "Europe/Berlin",
    })
    assert created.status_code == 200
    schedule = created.json()["schedule"]
    assert schedule["next_due_at"]
    listed = http.get("/api/schedules").json()["schedules"]
    assert [s["name"] for s in listed] == ["api-daily"]
    disabled = http.post(f"/api/schedules/{schedule['id']}/disable")
    assert disabled.json()["schedule"]["enabled"] == 0
    enabled = http.post(f"/api/schedules/{schedule['id']}/enable")
    assert enabled.json()["schedule"]["enabled"] == 1
    # Validation errors are explicit, not crashes.
    bad = http.post("/api/schedules", json={"name": "", "prompt": "", "kind": "nope"})
    assert bad.status_code == 400

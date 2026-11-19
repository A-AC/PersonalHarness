"""E. Scheduling and heartbeat: due times, timezones, restarts, idempotency,
approval pause/resume, and heartbeat behavior."""

from __future__ import annotations

import datetime as dt

import pytest

from harness import util
from harness.tools import ToolSpec


def _past(minutes: float = 5) -> str:
    return util.iso(util.now_utc() - dt.timedelta(minutes=minutes))


def _future(minutes: float = 30) -> str:
    return util.iso(util.now_utc() + dt.timedelta(minutes=minutes))


def test_scheduled_prompt_submitted_at_due_time_and_timezone(make_app):
    app = make_app.build(default="ok")
    app.scheduler.create_schedule(
        name="daily-standup", prompt="Summarize open work.",
        kind="daily", time_of_day="09:00", tz="America/Mexico_City",
    )
    schedule = app.scheduler.list_schedules()[0]
    assert schedule["next_due_at"]
    local = util.parse_iso(schedule["next_due_at"])
    # 09:00 in Mexico City is 15:00 UTC (CST, UTC-6).
    assert local.hour == 15 and local.minute == 0
    # Not due yet: nothing submitted.
    assert app.scheduler.submit_due() == []
    # Force it due and submit.
    app.db.execute("UPDATE schedules SET next_due_at=? WHERE id=?",
                   (_past(), schedule["id"]))
    submitted = app.scheduler.submit_due()
    assert len(submitted) == 1


@pytest.mark.parametrize("kwargs", [
    {"kind": "daily"},
    {"kind": "daily", "time_of_day": "25:00"},
    {"kind": "weekly", "time_of_day": "12:00"},
    {"kind": "weekly", "weekday": "mon"},
    {"kind": "interval"},
    {"kind": "interval", "interval_s": 0},
])
def test_schedule_requires_explicit_kind_specific_fields(make_app, kwargs):
    app = make_app.build(default="ok")
    with pytest.raises(ValueError):
        app.scheduler.create_schedule(
            name="invalid", prompt="do not schedule", **kwargs,
        )
    assert app.scheduler.list_schedules() == []


def test_schedule_local_due_uses_schedule_timezone(make_app):
    app = make_app.build(default="ok")
    schedule = app.scheduler.create_schedule(
        name="noon-report", prompt="research", kind="daily",
        time_of_day="12:00", tz="America/Mexico_City",
    )
    local = dt.datetime.fromisoformat(app.scheduler.next_due_local(schedule))
    utc = util.parse_iso(schedule["next_due_at"])
    assert (local.hour, local.minute) == (12, 0)
    assert local.utcoffset() == dt.timedelta(hours=-6)
    assert (utc.hour, utc.minute) == (18, 0)


def test_task_schedule_can_explicitly_target_an_active_domain(make_app):
    app = make_app.build(default="ok")
    owner = app.tasks.create(objective="create AI schedule", origin="user")
    domain = app.domains.create(
        "dom_ai", name="ai_research", description="AI research",
        created_by="test",
    )
    schedule = app.scheduler.create_for_task(
        owner["id"], name="AI report", prompt="Report AI news",
        kind="daily", time_of_day="12:00", target_domain_id=domain["id"],
    )
    assert schedule["domain_id"] == domain["id"]
    with pytest.raises(ValueError):
        app.scheduler.create_for_task(
            owner["id"], name="bad", prompt="bad", kind="daily",
            time_of_day="12:00", target_domain_id="missing",
        )


def test_schedules_survive_restart(make_app):
    app = make_app.build(default="ok")
    app.scheduler.create_schedule(
        name="weekly-review", prompt="Review open work.",
        kind="weekly", weekday="mon", time_of_day="18:30",
        tz="Europe/Berlin",
    )
    app2 = make_app.restart()
    schedules = app2.scheduler.list_schedules()
    assert len(schedules) == 1
    assert schedules[0]["name"] == "weekly-review"
    assert schedules[0]["tz"] == "Europe/Berlin"
    assert schedules[0]["next_due_at"]


def test_missed_runs_follow_policy_run_once(make_app):
    app = make_app.build(default="ok")
    schedule = app.scheduler.create_schedule(
        name="hourly", prompt="check", kind="interval", interval_s=3600,
    )
    # Simulate four missed hourly slots.
    hours_ago = util.now_utc() - dt.timedelta(hours=4)
    app.db.execute("UPDATE schedules SET next_due_at=? WHERE id=?",
                   (util.iso(hours_ago), schedule["id"]))
    submitted = app.scheduler.submit_due()
    assert len(submitted) == 1  # coalesced to one run
    occurrences = app.db.query(
        "SELECT status, note FROM schedule_occurrences WHERE schedule_id=?",
        (schedule["id"],),
    )
    skipped = [o for o in occurrences if o["status"] == "skipped"]
    assert len(skipped) == 4  # the four older missed slots
    assert all("missed" in (o["note"] or "") for o in skipped)
    # The next due time is in the future again.
    schedule = app.scheduler.get_schedule(schedule["id"])
    assert util.parse_iso(schedule["next_due_at"]) > util.now_utc()


def test_missed_runs_policy_skip(make_app):
    app = make_app.build(default="ok")
    app.config.set("scheduler.missed_policy", "skip")
    schedule = app.scheduler.create_schedule(
        name="hourly", prompt="check", kind="interval", interval_s=3600,
    )
    hours_ago = util.now_utc() - dt.timedelta(hours=3)
    app.db.execute("UPDATE schedules SET next_due_at=? WHERE id=?",
                   (util.iso(hours_ago), schedule["id"]))
    assert app.scheduler.submit_due() == []
    occurrences = app.db.query(
        "SELECT status FROM schedule_occurrences WHERE schedule_id=?",
        (schedule["id"],),
    )
    assert all(o["status"] == "skipped" for o in occurrences)


def test_repeated_ticks_do_not_duplicate_occurrence(make_app):
    app = make_app.build(default="ok")
    schedule = app.scheduler.create_schedule(
        name="once-only", prompt="one shot", kind="once", at_ts=_past(),
    )
    first = app.scheduler.submit_due()
    second = app.scheduler.submit_due()  # duplicate tick / restart
    third = app.scheduler.submit_due()
    assert len(first) == 1 and not second and not third
    assert app.scheduler.get_schedule(schedule["id"])["enabled"] == 0


def test_once_schedule_across_restart_not_resubmitted(make_app):
    app = make_app.build(default="ok")
    schedule = app.scheduler.create_schedule(
        name="once-check", prompt="single run", kind="once", at_ts=_past(),
    )
    submitted = app.scheduler.submit_due()
    assert len(submitted) == 1
    task_id = submitted[0]["task"]["id"]
    app.tasks.set_status(task_id, "completed", reason="done")
    # Restart: the completed occurrence must not run again.
    app2 = make_app.restart()
    assert app2.scheduler.submit_due() == []
    occurrences = app2.db.query(
        "SELECT status FROM schedule_occurrences WHERE schedule_id=?",
        (schedule["id"],),
    )
    assert occurrences[0]["status"] == "submitted"
    assert app2.tasks.get(task_id)["status"] == "completed"


def test_crash_between_task_create_and_occurrence_mark_does_not_duplicate(make_app):
    app = make_app.build(default="ok")
    app.scheduler.create_schedule(
        name="once", prompt="do the once-only work", kind="once",
        at_ts=_past(5),
    )
    submitted = app.scheduler.submit_due()
    assert len(submitted) == 1
    task = submitted[0]["task"]
    occurrence_id = submitted[0]["occurrence_id"]
    # Simulate the old crash window: the task exists, but the occurrence is
    # still pending and has no task link.
    app.db.execute(
        "UPDATE schedule_occurrences SET status='pending', submitted_at=NULL,"
        " task_id=NULL WHERE id=?",
        (occurrence_id,),
    )
    app.scheduler.heartbeat()
    scheduled = [t for t in app.tasks.list_recent(limit=100)
                 if t["origin"] == "schedule"]
    assert len(scheduled) == 1
    assert scheduled[0]["id"] == task["id"]
    assert app.db.query_one(
        "SELECT COUNT(*) c FROM runs WHERE task_id=?", (task["id"],)
    )["c"] == 1
    occurrence = app.db.query_one(
        "SELECT * FROM schedule_occurrences WHERE id=?", (occurrence_id,)
    )
    assert occurrence["status"] == "submitted"
    assert occurrence["task_id"] == task["id"]


def test_scheduled_task_approval_pauses_and_resumes_same_run(make_app):
    scripts = [
        {"tool_calls": [{"name": "create_domain", "arguments": {
            "name": "Logs", "description": "log work",
            "reason": "durable area",
            "keywords": ["log", "logs"],
            "examples": ["review server logs", "search log files"]}}]},
        {"content": "Domain created; scheduled work continues."},
    ]
    app = make_app.build(scripts=scripts)
    app.scheduler.create_schedule(
        name="setup", prompt="Create the Logs domain for the log work.",
        kind="once", at_ts=_past(),
    )
    submitted = app.scheduler.submit_due()
    item = submitted[0]
    task = item["task"]
    result = app.runtime.start_run(
        task, {"kind": "schedule", "name": "setup"},
        trigger_text=task["objective"],
    )
    assert result.status == "waiting_approval"
    approval = app.approvals.pending()[0]
    # The approval survives a restart.
    app2 = make_app.restart(scripts=[
        {"content": "Domain created; scheduled work continues."},
    ])
    assert app2.approvals.pending()[0]["id"] == approval["id"]
    # Approving resumes the SAME task and run; it does not re-run the prompt.
    app2.approvals.decide(approval["id"], "approve", decided_by="web")
    resumed = app2.runtime.resume_after_approval(approval, "web")
    assert resumed.status == "completed"
    assert resumed.task_id == task["id"]
    assert resumed.run_id == result.run_id
    # The create_domain call executed exactly once.
    executions = app2.db.query_one(
        "SELECT COUNT(*) c FROM tool_executions WHERE task_id=? AND"
        " status='succeeded'",
        (task["id"],),
    )["c"]
    assert executions == 1


def _register_fake_web(app, calls):
    def search(ctx, arguments):
        calls.append(dict(arguments))
        return {
            "results": [{
                "title": "Current AI news", "url": "https://example.com/current",
                "snippet": "Current developments",
            }],
            "sources": ["https://example.com/current"],
        }
    app.tools.register(ToolSpec(
        name="web_search", description="fake web search",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "categories": {"type": "string"},
                "max_results": {"type": "integer"},
                "time_range": {"type": "string"},
            },
            "required": ["query"],
        },
        risk="low", handler=search, external_egress=True,
        read_only=True, untrusted_output=True,
    ))


def test_unattended_schedule_runs_only_approved_research_capabilities(make_app):
    calls = []
    app = make_app.build(scripts=[])
    _register_fake_web(app, calls)
    owner = app.tasks.create(objective="own the report", origin="user")
    policy = {
        "web_queries": [
            {"query": "local AI news {current_year}", "max_results": 5,
             "categories": "", "time_range": "week"},
            {"query": "frontier AI news {current_date}", "max_results": 5,
             "categories": "", "time_range": "week"},
        ],
        "allow_task_notes": True,
    }
    app.scheduler.create_for_task(
        owner["id"], name="daily-news", prompt="Report current AI news",
        kind="once", at_ts=_past(), unattended=policy, tz="UTC",
    )
    item = app.scheduler.submit_due()[0]
    task = item["task"]
    scheduled_for = util.parse_iso(app.tasks.meta(task["id"])["scheduled_for"])
    year = scheduled_for.strftime("%Y")
    date = scheduled_for.strftime("%Y-%m-%d")
    app.model.scripts.extend([
        {"tool_calls": [{"name": "web_search", "arguments": {
            "query": f"local AI news {year}", "time_range": "week",
        }}]},
        {"tool_calls": [{"name": "web_search", "arguments": {
            "query": f"frontier AI news {date}", "time_range": "week",
        }}]},
        {"tool_calls": [{"name": "update_task_notes", "arguments": {
            "notes": "Current report notes",
        }}]},
        {"tool_calls": [{"name": "update_task_notes", "arguments": {
            "notes": "This second write must be denied",
        }}]},
        {"content": "Report with https://example.com/current"},
    ])
    result = app.runtime.start_run(
        task, {"kind": "schedule", "name": "daily-news"},
    )
    assert result.status == "completed"
    assert app.approvals.pending() == []
    assert len(calls) == 2
    notes = app.tasks.meta(task["id"])
    assert notes["notes"] == "Current report notes"
    assert notes["notes_tainted"]
    toolset = util.loads(app.db.query_one(
        "SELECT meta FROM runs WHERE id=?", (result.run_id,)
    )["meta"], {})["toolset"]
    assert set(toolset["names"]) == {"web_search", "update_task_notes"}
    transcript = util.loads(app.db.query_one(
        "SELECT transcript FROM runs WHERE id=?", (result.run_id,)
    )["transcript"], [])
    assert any(
        "update_task_notes" in str(message)
        and "unattended" in str(message).lower()
        for message in transcript if message.get("role") == "tool"
    )
    assert "This second write must be denied" not in notes["notes"]


def test_unattended_schedule_denies_unapproved_query_without_approval(make_app):
    calls = []
    app = make_app.build(scripts=[])
    _register_fake_web(app, calls)
    owner = app.tasks.create(objective="own the report", origin="user")
    app.scheduler.create_for_task(
        owner["id"], name="daily-news", prompt="Report current AI news",
        kind="once", at_ts=_past(), unattended={
            "web_queries": [{"query": "approved AI news {current_year}"}],
            "allow_task_notes": False,
        },
    )
    task = app.scheduler.submit_due()[0]["task"]
    app.model.scripts.extend([
        {"tool_calls": [{"name": "web_search", "arguments": {
            "query": "unapproved private calendar details",
        }}]},
        {"content": "I could not use an unapproved query."},
    ])
    result = app.runtime.start_run(
        task, {"kind": "schedule", "name": "daily-news"},
    )
    assert result.status == "completed"
    assert calls == []
    assert app.approvals.pending() == []
    assert not any(
        event["type"] == "approval.created" for event in app.events.list()
    )


def test_schedule_tool_result_contains_local_time_and_target_domain(make_app):
    app = make_app.build(scripts=[])
    domain = app.domains.create(
        "dom_ai", name="ai_research", description="AI research",
        created_by="test",
    )
    app.model.scripts.extend([
        {"tool_calls": [{"name": "schedule_task", "arguments": {
            "name": "AI report", "prompt": "Report AI news", "kind": "daily",
            "time_of_day": "12:00", "tz": "America/Mexico_City",
            "domain": "ai_research",
        }}]},
        {"content": "Schedule created."},
    ])
    result = app.runtime.submit_user_message("Schedule the AI report")
    assert result.status == "waiting_approval"
    approval = app.approvals.pending()[0]
    app.approvals.decide(approval["id"], "approve", decided_by="web")
    resumed = app.runtime.resume_after_approval(approval, "web")
    assert resumed.status == "completed"
    execution = app.db.query_one(
        "SELECT result FROM tool_executions WHERE run_id=? AND tool='schedule_task'"
        " AND status='succeeded'", (result.run_id,),
    )
    payload = util.loads(execution["result"], {})
    local = dt.datetime.fromisoformat(payload["next_due_at_local"])
    assert (local.hour, local.minute) == (12, 0)
    assert payload["timezone"] == "America/Mexico_City"
    assert payload["time_of_day"] == "12:00"
    assert payload["domain_id"] == domain["id"]
    assert util.parse_iso(payload["next_due_at"]).hour == 18


def test_underspecified_schedule_is_denied_before_approval(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "schedule_task", "arguments": {
            "name": "AI report", "prompt": "Report AI news", "kind": "daily",
        }}]},
        {"content": "I need a specific time first."},
    ])
    result = app.runtime.submit_user_message("Schedule the AI report")
    assert result.status == "completed"
    assert app.approvals.pending() == []
    assert app.scheduler.list_schedules() == []
    transcript = util.loads(app.db.query_one(
        "SELECT transcript FROM runs WHERE id=?", (result.run_id,)
    )["transcript"], [])
    assert any(
        "time_of_day" in str(message.get("content") or "")
        and "denied" in str(message.get("content") or "")
        for message in transcript if message.get("role") == "tool"
    )


def test_heartbeat_does_not_overlap(make_app):
    app = make_app.build(default="ok")
    # Hold the tick lock; a second heartbeat must skip, not run concurrently.
    assert app.scheduler._tick_lock.acquire(blocking=False)
    try:
        report = app.scheduler.heartbeat()
        assert report.get("skipped") == "another tick is running"
    finally:
        app.scheduler._tick_lock.release()


def test_heartbeat_retries_model_failure_after_cooldown(make_app):
    app = make_app.build(scripts=[{"error": "down"}])
    app.config.set("scheduler.retry_cooldown_s", 0)
    app.config.set("scheduler.max_task_retries", 2)
    result = app.runtime.submit_user_message("do the work")
    assert result.status == "failed"
    task = app.tasks.get(result.task_id)
    assert task["status"] == "paused"
    # Heartbeat retries with the recovered model; the transcript resumes.
    app.model.scripts.append({"content": "Finished after retry."})
    report = app.scheduler.heartbeat()
    assert task["id"] in report["retried"]
    assert app.tasks.get(task["id"])["status"] == "completed"
    # The retry budget is respected: no third retry.
    app.model.scripts.append({"error": "down again"})
    app.runtime.submit_user_message("more work")
    task2 = app.db.query_one(
        "SELECT * FROM tasks WHERE objective='more work'"
    )
    report2 = app.scheduler.heartbeat()
    # 'more work' also paused; retry count starts at 0 -> retried once
    tasks_retried = [t for t in report2.get("retried", [])]
    assert task2["id"] in tasks_retried


def test_heartbeat_notifies_pending_approval_once(make_app):
    app = make_app.build(default="ok")
    app.config.set("scheduler.approval_notify_s", 0)
    task = app.tasks.create(objective="wait", origin="user")
    run_id = util.new_id("run")
    app.db.execute(
        "INSERT INTO runs(id, task_id, trigger, status, started_at,"
        " transcript) VALUES (?,?,?,?,?,?)",
        (run_id, task["id"], "user", "running", util.iso(),
         util.dumps([{"role": "user", "content": "x"}])),
    )
    app.approvals.create(
        task_id=task["id"], run_id=run_id, action="tool", tool="some_tool",
        arguments={"x": 1}, risk="confirm",
    )
    app.scheduler.heartbeat()
    notes_after_first = [m for m in app.messages.list_visible()
                         if "waiting for your approval" in m["content"]]
    assert len(notes_after_first) == 1
    app.scheduler.heartbeat()  # second pass: no duplicate reminder
    notes_after_second = [m for m in app.messages.list_visible()
                          if "waiting for your approval" in m["content"]]
    assert len(notes_after_second) == 1

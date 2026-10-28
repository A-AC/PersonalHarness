"""Regression tests for the reliability failures found in live use.

Each test pins one root cause: empty model replies, approval classification,
duplicate approvals, transcript protocol shape, routing order, context
visibility, and heartbeat over-processing.
"""

from __future__ import annotations

import datetime as dt
import json

from harness import transcript, util
from harness.model import ToolCall


def _say(app, text, source="web", domain_id=None):
    return app.runtime.submit_user_message(
        text, source=source, domain_id=domain_id
    )


def _register_confirm_tool(app, name="mcp_test_send"):
    from harness.tools import ToolSpec

    def handler(ctx, args):
        return {"done": True, "echo": args.get("text")}

    app.tools.register(ToolSpec(
        name=name,
        description="send something somewhere (external effect)",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        risk="confirm",
        handler=handler,
    ))


def _manual_run(app, task, status="running", transcript_rows=None):
    run_id = util.new_id("run")
    app.db.execute(
        "INSERT INTO runs(id, task_id, trigger, status, started_at, transcript)"
        " VALUES (?,?,?,?,?,?)",
        (run_id, task["id"], "user", status, util.iso(),
         util.dumps(transcript_rows or [
             {"role": "system", "content": "system"},
             {"role": "user", "content": "go"},
         ])),
    )
    return run_id


# ---------------------------------------------------------------------------
# Transcript hygiene
# ---------------------------------------------------------------------------

def test_repair_removes_mid_transcript_system_message():
    repaired, repairs = transcript.repair([
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "recovery note"},
        {"role": "assistant", "content": "ok"},
    ])
    assert repaired[0]["role"] == "system"
    assert all(m["role"] != "system" for m in repaired[1:])
    assert any("system" in note for note in repairs)
    assert "[system note] recovery note" in repaired[2]["content"]


def test_repair_inserts_result_for_unanswered_tool_call():
    repaired, repairs = transcript.repair([
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "x", "arguments": "{}"},
        }]},
    ])
    assert repaired[-1]["role"] == "tool"
    assert repaired[-1]["tool_call_id"] == "call_1"
    assert json.loads(repaired[-1]["content"])["status"] == "unknown"
    assert any("placeholder" in note for note in repairs)


def test_compact_keeps_the_current_turn_and_fits_budget():
    messages = [{"role": "system", "content": "rules " + "x" * 4000}]
    for index in range(80):
        messages.append({"role": "user", "content": f"old {index} " + "y" * 400})
        messages.append({"role": "assistant", "content": "ack " + "z" * 400})
    messages.append({"role": "user", "content": "CURRENT QUESTION"})
    compacted, notes = transcript.compact(messages, 1500)
    assert compacted[0]["role"] == "system"
    assert compacted[-1]["content"] == "CURRENT QUESTION"
    assert transcript.estimate_tokens(compacted) <= 1500
    assert notes


# ---------------------------------------------------------------------------
# Approvals: duplicates, multiple pending, placeholder replacement
# ---------------------------------------------------------------------------

def test_duplicate_create_domain_proposal_is_refused_not_queued(make_app):
    app = make_app.build(default="ok")
    task = app.tasks.create(objective="astronomy", origin="user")
    run_id = _manual_run(app, task)
    app.approvals.create(
        task_id=task["id"], run_id=run_id, action="create_domain",
        tool="create_domain",
        arguments={"name": "Astronomy", "description": "telescopes",
                   "reason": "recurring"},
        risk="confirm", tool_call_id="call_1",
    )
    call = ToolCall(
        id="call_2", name="create_domain",
        arguments={"name": "astronomy", "description": "telescopes",
                   "reason": "recurring"},
        raw_arguments="{}",
    )
    outcome = app.runtime._handle_tool_call(
        app.runtime._run_row(run_id), task, call
    )
    assert outcome["status"] == "denied"
    assert "already pending" in outcome["error"]
    assert len(app.approvals.pending()) == 1


def test_create_domain_for_existing_domain_needs_no_approval(make_app):
    app = make_app.build(default="ok")
    existing = app.domains.create(
        util.new_id("dom"), name="Finance", description="money",
        created_by="test",
    )
    task = app.tasks.create(objective="finance", origin="user")
    run_id = _manual_run(app, task)
    call = ToolCall(
        id="call_1", name="create_domain",
        arguments={"name": "Finance", "description": "money",
                   "reason": "again"},
        raw_arguments="{}",
    )
    outcome = app.runtime._handle_tool_call(
        app.runtime._run_row(run_id), task, call
    )
    assert outcome["status"] == "ok"
    assert outcome["result"]["already_exists"] == "Finance"
    assert outcome["result"]["domain_id"] == existing["id"]
    assert app.approvals.pending() == []


def test_awaiting_approval_placeholder_is_replaced(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "mcp_test_send",
                         "arguments": {"text": "report"}}]},
        {"content": "Report sent."},
    ])
    _register_confirm_tool(app)
    result = _say(app, "send the report")
    assert result.status == "waiting_approval"
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    rows = util.loads(run["transcript"])
    tool_rows = [m for m in rows if m.get("role") == "tool"]
    assert len(tool_rows) == 1
    assert json.loads(tool_rows[0]["content"])["status"] == "awaiting_approval"

    approval = app.approvals.pending()[0]
    app.approvals.decide(approval["id"], "approve", decided_by="web")
    app.runtime.resume_after_approval(approval, "web")
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    rows = util.loads(run["transcript"])
    tool_rows = [
        m for m in rows
        if m.get("role") == "tool" and m.get("tool_call_id") == approval["tool_call_id"]
    ]
    assert len(tool_rows) == 1
    assert json.loads(tool_rows[0]["content"]).get("status") != "awaiting_approval"
    assert "echo" in tool_rows[0]["content"]


def test_ambiguous_short_reply_gets_a_visible_question(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "mcp_test_send",
                         "arguments": {"text": "x"}}]},
    ])
    _register_confirm_tool(app)
    _say(app, "send x")
    approval = app.approvals.pending()[0]
    app.model.scripts.append({"content": "ambiguous"})
    result = _say(app, "hmm maybe")
    assert result.status == "waiting_approval"
    notes = [m for m in app.messages.list_visible() if m["role"] == "note"]
    assert any("not a clear yes or no" in m["content"] for m in notes)
    assert app.approvals.pending()[0]["id"] == approval["id"]


# ---------------------------------------------------------------------------
# Routing and context
# ---------------------------------------------------------------------------

def test_routing_precedes_resume_candidate_selection(make_app):
    app = make_app.build(scripts=[])
    homelab = app.domains.create(
        util.new_id("dom"), name="Homelab",
        description="servers, network, nas, rack", created_by="test",
    )
    task = app.tasks.create(
        objective="check the nas backup status",
        domain_id=homelab["id"], origin="user",
    )
    app.tasks.set_status(task["id"], "waiting_user", reason="waiting")
    app.model.scripts.append({"content": "Checked: healthy."})
    result = _say(app, "check the nas backup status in the homelab")
    assert result.task_id == task["id"]
    assert app.tasks.get(task["id"])["domain_id"] == homelab["id"]


def test_context_lists_pending_approvals_from_other_tasks(make_app):
    app = make_app.build(default="ok")
    other = app.tasks.create(objective="other work", origin="user")
    other_run = _manual_run(app, other, status="waiting_approval")
    approval = app.approvals.create(
        task_id=other["id"], run_id=other_run, action="tool",
        tool="mcp_mail_send", arguments={"text": "hello"}, risk="confirm",
    )
    result = _say(app, "what is this about?")
    assert result.status == "completed"
    system = app.model.calls[-1]["messages"][0]["content"]
    assert approval["id"] in system
    assert "another task" in system
    assert "Available domains" in system


# ---------------------------------------------------------------------------
# Heartbeat bounds and failure categories
# ---------------------------------------------------------------------------

def test_heartbeat_caps_background_runs_per_tick(make_app):
    app = make_app.build(default="ok")
    app.config.set("scheduler.max_runs_per_tick", 1)
    for index in range(2):
        schedule = app.scheduler.create_schedule(
            name=f"job-{index}", prompt=f"work {index}",
            kind="once", at_ts=util.iso(util.now_utc() - dt.timedelta(minutes=1)),
        )
        app.db.execute(
            "UPDATE schedules SET next_due_at=? WHERE id=?",
            (util.iso(util.now_utc() - dt.timedelta(minutes=1)),
             schedule["id"]),
        )
    first = app.scheduler.heartbeat()
    assert len(first["submitted"]) == 1
    assert first["deferred"] == 1
    second = app.scheduler.heartbeat()
    assert len(second["resumed"]) == 1


def test_heartbeat_skips_permanent_protocol_failures(make_app):
    app = make_app.build(scripts=[{"malformed": True}])
    result = _say(app, "do the work")
    assert result.status == "failed"
    task = app.tasks.get(result.task_id)
    assert task["status_reason"].startswith("model protocol failure")
    report = app.scheduler.heartbeat()
    assert task["id"] not in report["retried"]
    assert int(app.tasks.get(task["id"])["retry_count"]) == 0


def test_heartbeat_schedules_backoff_before_retrying(make_app):
    app = make_app.build(scripts=[{"error": "down"}])
    app.config.set("scheduler.retry_cooldown_s", 300)
    result = _say(app, "do the work")
    report = app.scheduler.heartbeat()
    assert report["retried"] == []
    task = app.tasks.get(result.task_id)
    assert task["next_retry_at"]
    assert util.parse_iso(task["next_retry_at"]) > util.now_utc()


# ---------------------------------------------------------------------------
# Server surface
# ---------------------------------------------------------------------------

def test_config_update_validates_unknown_keys(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    bad = http.post("/api/config", json={
        "group": "scheduler",
        "value": {"not_a_real_key": 1},
    })
    assert bad.status_code == 400
    good = http.post("/api/config", json={
        "group": "scheduler",
        "value": {"tick_interval_s": 45},
    })
    assert good.status_code == 200
    assert app.config.get("scheduler.tick_interval_s") == 45


def test_domain_patch_and_heartbeat_endpoints(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    domain = app.domains.create(
        util.new_id("dom"), name="Garden", description="plants",
        created_by="test",
    )
    patched = http.patch(f"/api/domains/{domain['id']}", json={
        "description": "plants, soil, irrigation",
    })
    assert patched.status_code == 200
    assert "irrigation" in patched.json()["domain"]["description"]
    heartbeat = http.post("/api/heartbeat")
    assert heartbeat.status_code == 200
    assert "report" in heartbeat.json()
    state = http.get("/api/state").json()
    assert state["counts"]["domains"] >= 1
    assert "persona" in state


def test_message_endpoint_accepts_explicit_domain(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    domain = app.domains.create(
        util.new_id("dom"), name="Finance", description="money",
        created_by="test",
    )
    response = http.post("/api/message", json={
        "text": "track this invoice", "domain_id": domain["id"],
    })
    assert response.status_code == 200
    task = app.tasks.get(response.json()["task_id"])
    assert task["domain_id"] == domain["id"]


def test_web_page_exposes_every_panel(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    page = http.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    for tab in ("chat", "tasks", "approvals", "domains", "persona",
                "settings", "schedules", "memory", "system"):
        assert f'data-tab="{tab}"' in page.text, tab
    # The page must talk to the same API the CLI uses, not a second store.
    assert "/api/state" in page.text
    assert "/api/config" in page.text
    assert "/api/domains" in page.text


def test_cancel_task_cancels_approvals_and_stops_its_run(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    task = app.tasks.create(objective="cancel me", origin="user")
    run_id = _manual_run(app, task, status="waiting_approval")
    approval = app.approvals.create(
        task_id=task["id"], run_id=run_id, action="tool",
        tool="mcp_test_send", arguments={"text": "x"}, risk="confirm",
    )
    response = http.post(f"/api/tasks/{task['id']}/cancel")
    assert response.status_code == 200
    assert app.tasks.get(task["id"])["status"] == "cancelled"
    assert app.approvals.get(approval["id"])["status"] == "cancelled"
    run = app.db.query_one("SELECT status FROM runs WHERE id=?", (run_id,))
    assert run["status"] == "failed"


def test_heartbeat_never_revives_a_terminal_task(make_app):
    app = make_app.build(default="ok")
    task = app.tasks.create(objective="ended work", origin="user")
    run_id = _manual_run(app, task, status="interrupted")
    # Simulate debris from an older build: cancelled task, interrupted run.
    app.tasks.set_status(task["id"], "cancelled", reason="cancelled by user")
    report = app.scheduler.heartbeat()
    assert task["id"] not in report["resumed"]
    run = app.db.query_one("SELECT status FROM runs WHERE id=?", (run_id,))
    assert run["status"] == "failed"
    assert app.tasks.get(task["id"])["status"] == "cancelled"

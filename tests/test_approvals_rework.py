"""Phase 8B: conversational approval revisions, edits, and risk overrides."""

from __future__ import annotations

import json
import re
import sqlite3

import pytest

from harness import util
from harness.approvals import ApprovalError
from harness.config import load_config
from harness.db import Database, SCHEMA_VERSION
from harness.tools import ToolSpec


def _register_confirm_tool(
    app,
    *,
    name: str = "mcp_test_send",
    side_effects: bool = False,
    summary_template: str = "",
):
    """Register one deterministic confirm-risk tool and record its calls."""
    received: list[dict] = []

    def handler(ctx, args):
        received.append(dict(args))
        return {"done": True, "echo": args.get("text")}

    app.tools.register(ToolSpec(
        name=name,
        description="send something somewhere (external effect)",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "recipient": {"type": "string"},
            },
            "required": ["text"],
        },
        risk="confirm",
        handler=handler,
        side_effects=side_effects,
        summary_template=summary_template,
    ))
    return received


def _proposal(
    text: str, recipient: str | None = None, tool: str = "mcp_test_send"
) -> dict:
    arguments: dict = {"text": text}
    if recipient is not None:
        arguments["recipient"] = recipient
    return {"tool_calls": [{"name": tool, "arguments": arguments}]}


def _run_transcript(app, run_id: str) -> list[dict]:
    row = app.db.query_one(
        "SELECT transcript FROM runs WHERE id=?", (run_id,)
    )
    return util.loads(row["transcript"] or "[]", [])


def _event_data(app, event_type: str) -> dict:
    row = app.db.query_one(
        "SELECT data FROM event_log WHERE type=? ORDER BY id DESC LIMIT 1",
        (event_type,),
    )
    assert row is not None
    return util.loads(row["data"] or "{}", {})


def test_feedback_reply_revises_and_reproposes(make_app):
    app = make_app.build(scripts=[
        _proposal("hello", "alice"),
        {"content": "ambiguous"},
        _proposal("hello", "bob"),
        {"content": "Ready with the revised recipient."},
    ])
    received = _register_confirm_tool(app)

    first_result = app.runtime.submit_user_message("send hello to alice")
    assert first_result.status == "waiting_approval"
    first = app.approvals.pending()[0]

    feedback_result = app.runtime.submit_user_message(
        "use a different recipient"
    )
    assert feedback_result.status == "waiting_approval"

    revised = app.approvals.get(first["id"])
    assert revised["status"] == "revised"
    execution = app.db.query_one(
        "SELECT status FROM tool_executions WHERE approval_id=?",
        (first["id"],),
    )
    assert execution["status"] == "denied"

    transcript = _run_transcript(app, first["run_id"])
    revision_results = [
        util.loads(message.get("content") or "", {})
        for message in transcript
        if message.get("role") == "tool"
        and "revision_requested" in message.get("content", "")
    ]
    assert len(revision_results) == 1
    assert "use a different recipient" in revision_results[0]["feedback"]

    pending = app.approvals.pending()
    assert len(pending) == 1
    assert pending[0]["supersedes_id"] == first["id"]
    assert pending[0]["status"] == "pending"
    assert pending[0]["arguments"]["recipient"] == "bob"
    assert not received


def test_unrelated_message_leaves_approval_pending_and_starts_new_task(
    make_app,
):
    app = make_app.build(scripts=[
        _proposal("hello"),
        {"content": "ambiguous"},
    ])
    _register_confirm_tool(app)
    app.runtime.submit_user_message("send hello")
    approval = app.approvals.pending()[0]

    app.runtime.submit_user_message("rain tomorrow in Oslo")

    assert app.approvals.get(approval["id"])["status"] == "pending"
    assert not app.db.query(
        "SELECT id FROM approvals WHERE status='revised'"
    )
    assert app.approvals.pending()[0]["id"] == approval["id"]


def test_question_while_pending_is_not_feedback(make_app):
    app = make_app.build(scripts=[
        _proposal("hello"),
        {"content": "ambiguous"},
        {"content": "It would send the prepared message."},
    ])
    _register_confirm_tool(app)
    app.runtime.submit_user_message("send hello")
    approval = app.approvals.pending()[0]

    app.runtime.submit_user_message("what would that do?")

    assert app.approvals.get(approval["id"])["status"] == "pending"
    assert not app.db.query(
        "SELECT id FROM approvals WHERE status='revised'"
    )


def test_superseded_approval_can_never_execute(make_app):
    app = make_app.build(scripts=[
        _proposal("original"),
        {"content": "Sent the edited message."},
    ])
    received = _register_confirm_tool(app)
    app.runtime.submit_user_message("send original")
    old = app.approvals.pending()[0]

    replacement = app.approvals.supersede(
        old["id"],
        arguments={"text": "replacement"},
        reason="confirm-risk tool: mcp_test_send (edited by user)",
        risk="confirm",
        decided_by="edit:test",
    )

    assert app.approvals.claim_for_execution(old["id"]) is None
    with pytest.raises(ApprovalError):
        app.approvals.decide(
            old["id"], "approve", decided_by="test"
        )
    app.approvals.decide(
        replacement["id"], "approve", decided_by="test"
    )
    result = app.runtime.resume_after_approval(replacement, "test")
    assert result.status == "completed"
    assert received == [{"text": "replacement"}]


def test_edit_executes_exactly_the_edited_arguments(make_app):
    app = make_app.build(scripts=[
        _proposal("original"),
        {"content": "Sent the edited message."},
    ])
    received = _register_confirm_tool(app)
    app.runtime.submit_user_message("send original")
    old = app.approvals.pending()[0]

    result = app.runtime.edit_approval(
        old["id"], {"text": "edited"}, "web"
    )

    assert result.status == "completed"
    assert received == [{"text": "edited"}]
    assert app.approvals.get(old["id"])["status"] == "superseded"
    replacement = app.db.query_one(
        "SELECT id FROM approvals WHERE supersedes_id=?", (old["id"],)
    )
    assert replacement is not None
    assert app.approvals.get(replacement["id"])["status"] == "executed"
    event = _event_data(app, "approval.edited")
    assert event["diff"]
    assert event["diff"]["text"]["to"] == "edited"

    transcript = _run_transcript(app, old["run_id"])
    tool_messages = [
        util.loads(message.get("content") or "", {})
        for message in transcript
        if message.get("role") == "tool"
    ]
    assert any(
        message.get("user_edited_arguments") == {"text": "edited"}
        for message in tool_messages
    )


def test_invalid_edit_changes_nothing(make_app):
    app = make_app.build(scripts=[_proposal("original")])
    received = _register_confirm_tool(app)
    app.runtime.submit_user_message("send original")
    old = app.approvals.pending()[0]

    invalid = [
        {"text": "edited", "unknown": "value"},
        {},
        {"text": 123},
    ]
    for arguments in invalid:
        with pytest.raises(ValueError):
            app.runtime.edit_approval(old["id"], arguments, "web")

    assert app.approvals.get(old["id"])["status"] == "pending"
    assert not received
    assert not app.db.query(
        "SELECT id FROM tool_executions WHERE status='succeeded'"
    )
    assert len(app.db.query("SELECT id FROM approvals")) == 1


def test_approval_summary_uses_template_then_fallback(make_app):
    app = make_app.build(default="ok")
    _register_confirm_tool(
        app, name="mcp_test_template", summary_template="Send {text} now"
    )
    _register_confirm_tool(app, name="mcp_test_plain")
    task = app.tasks.create(objective="summaries", origin="user")
    run_id = util.new_id("run")
    templated = app.approvals.create(
        task_id=task["id"], run_id=run_id, action="tool",
        tool="mcp_test_template", arguments={"text": "hello"},
        risk="confirm",
    )
    plain = app.approvals.create(
        task_id=task["id"], run_id=run_id, action="tool",
        tool="mcp_test_plain", arguments={"text": "hello"},
        risk="confirm",
    )

    assert app.runtime.summary_for(templated) == "Send hello now"
    assert app.runtime.summary_for(plain) == app.approvals.describe(plain)


def test_always_allow_remember_requires_safe_untainted_tool(make_app):
    app = make_app.build(scripts=[
        _proposal("first"),
        {"content": "Sent."},
        _proposal("second"),
        {"content": "Sent again."},
    ])
    received = _register_confirm_tool(app)
    app.runtime.submit_user_message("send first")
    approval = app.approvals.pending()[0]

    result = app.runtime.decide_approval(
        approval["id"], "approve", remember=True
    )

    assert result.status == "completed"
    assert received == [{"text": "first"}]
    assert app.config.get("tools.risk_overrides")["mcp_test_send"] == "low"
    persisted = load_config(app.data_dir)
    assert persisted.get("tools.risk_overrides")["mcp_test_send"] == "low"

    later = app.runtime.submit_user_message("send second")
    assert later.status == "completed"
    assert not app.approvals.pending()
    assert received[-1] == {"text": "second"}

    side_app = make_app.build(scripts=[
        _proposal("effect", tool="mcp_test_effect")
    ])
    _register_confirm_tool(
        side_app, name="mcp_test_effect", side_effects=True
    )
    side_app.runtime.submit_user_message("send effect")
    side_approval = side_app.approvals.pending()[0]
    with pytest.raises(ValueError):
        side_app.runtime.decide_approval(
            side_approval["id"], "approve", remember=True
        )
    assert "mcp_test_effect" not in (
        side_app.config.get("tools.risk_overrides") or {}
    )
    side_app.runtime.cancel_task(side_approval["task_id"])

    tainted_app = make_app.build(scripts=[
        _proposal("tainted", tool="mcp_test_tainted")
    ])
    _register_confirm_tool(tainted_app, name="mcp_test_tainted")
    tainted_app.runtime.submit_user_message("send tainted")
    tainted = tainted_app.approvals.pending()[0]
    tainted_app.runtime._mark_tainted(
        tainted["run_id"], "mcp_test_tainted", "test", "untrusted:test"
    )
    with pytest.raises(ValueError):
        tainted_app.runtime.decide_approval(
            tainted["id"], "approve", remember=True
        )
    assert "mcp_test_tainted" not in (
        tainted_app.config.get("tools.risk_overrides") or {}
    )


def test_skill_writes_are_side_effecting_and_not_bulk_safe(make_app, client):
    app = make_app.build(scripts=[
        {"tool_calls": [{
            "name": "propose_skill",
            "arguments": {
                "name": "weekly review",
                "description": "run a weekly review",
                "body": "1. Gather open tasks.",
            },
        }]},
        {"content": "Draft proposed."},
    ])
    result = app.runtime.submit_user_message("propose a weekly review skill")
    assert result.status == "waiting_approval"

    approval = client(app).get("/api/approvals").json()["approvals"][0]
    assert approval["tool"] == "propose_skill"
    # Skill rows are durable state, so the UI must not offer always-allow or
    # include the approval in "Approve all safe".
    assert approval["side_effects"] is True
    with pytest.raises(ValueError):
        app.runtime.decide_approval(
            approval["id"], "approve", remember=True
        )
    assert app.approvals.get(approval["id"])["status"] == "pending"


def test_cue_words_inside_other_words_are_not_revision_feedback(make_app):
    app = make_app.build(scripts=[
        _proposal("hello"),
        {"content": "ambiguous"},
    ])
    _register_confirm_tool(app)
    app.runtime.submit_user_message("send hello")
    approval = app.approvals.pending()[0]

    # "exchange" contains "change", but it is a new topic, not a change
    # request for the pending action.
    app.runtime.submit_user_message("exchange rates for tomorrow")

    assert app.approvals.get(approval["id"])["status"] == "pending"
    assert not app.db.query("SELECT id FROM approvals WHERE status='revised'")


def test_risk_override_revoke_restores_confirm(make_app):
    app = make_app.build(default="ok")
    _register_confirm_tool(app)

    app.tools.set_risk_override("mcp_test_send", "low")
    assert app.tools.resolve("mcp_test_send").risk == "low"
    assert app.tools.risk_override("mcp_test_send") == "low"

    app.tools.set_risk_override("mcp_test_send", None)
    assert app.tools.resolve("mcp_test_send").risk == "confirm"
    assert app.tools.risk_override("mcp_test_send") is None
    assert "mcp_test_send" not in (
        app.config.get("tools.risk_overrides") or {}
    )


def test_api_approval_decisions(make_app, client):
    app = make_app.build(scripts=[
        _proposal("original"),
        {"content": "Sent the edited message."},
    ])
    received = _register_confirm_tool(app)
    app.runtime.submit_user_message("send original")
    approval = app.approvals.pending()[0]
    http = client(app)

    listing = http.get("/api/approvals")
    assert listing.status_code == 200
    payload = listing.json()["approvals"][0]
    assert payload["summary"]
    assert payload["schema"]["properties"]["text"]["type"] == "string"
    assert payload["editable"] is True
    assert payload["supersedes_id"] is None
    assert payload["feedback"] == ""

    edited = http.post(
        f"/api/approvals/{approval['id']}/edit",
        json={"arguments": {"text": "edited"}},
    )
    assert edited.status_code == 200
    assert received == [{"text": "edited"}]
    assert edited.json()["summary"]

    revise_app = make_app.build(scripts=[
        _proposal("original"),
        _proposal("revised"),
    ])
    _register_confirm_tool(revise_app)
    revise_app.runtime.submit_user_message("send original")
    revise_approval = revise_app.approvals.pending()[0]
    revise_http = client(revise_app)
    revised = revise_http.post(
        f"/api/approvals/{revise_approval['id']}/revise",
        json={"feedback": "use a shorter message"},
    )
    assert revised.status_code == 200
    assert revised.json()["status"] == "waiting_approval"
    assert revise_app.approvals.get(revise_approval["id"])["status"] == (
        "revised"
    )
    revise_app.runtime.cancel_task(revise_approval["task_id"])

    reject_app = make_app.build(scripts=[
        _proposal("original"),
        _proposal("revised"),
    ])
    _register_confirm_tool(reject_app)
    reject_app.runtime.submit_user_message("send original")
    reject_approval = reject_app.approvals.pending()[0]
    reject_http = client(reject_app)
    rejected = reject_http.post(
        f"/api/approvals/{reject_approval['id']}/reject",
        json={"feedback": "use another recipient"},
    )
    assert rejected.status_code == 200
    assert reject_app.approvals.get(reject_approval["id"])["status"] == (
        "revised"
    )
    reject_app.runtime.cancel_task(reject_approval["task_id"])

    remember_app = make_app.build(scripts=[
        _proposal("original"),
        {"content": "Sent."},
    ])
    _register_confirm_tool(remember_app)
    remember_app.runtime.submit_user_message("send original")
    remember_approval = remember_app.approvals.pending()[0]
    remember_http = client(remember_app)
    remembered = remember_http.post(
        f"/api/approvals/{remember_approval['id']}/approve",
        json={"remember": True},
    )
    assert remembered.status_code == 200
    assert remember_app.config.get("tools.risk_overrides")[
        "mcp_test_send"
    ] == "low"

    domain_app = make_app.build(default="ok")
    domain_task = domain_app.tasks.create(
        objective="create a domain", origin="user"
    )
    domain_run = util.new_id("run")
    domain_approval = domain_app.approvals.create(
        task_id=domain_task["id"], run_id=domain_run, action="create_domain",
        tool="create_domain", arguments={"name": "Example"},
        risk="confirm",
    )
    domain_http = client(domain_app)
    invalid = domain_http.post(
        f"/api/approvals/{domain_approval['id']}/edit",
        json={"arguments": {"unknown": "value"}},
    )
    assert invalid.status_code == 400
    assert domain_app.approvals.get(domain_approval["id"])["status"] == (
        "pending"
    )

    unknown = http.post(
        f"/api/approvals/{approval['id']}/unknown",
        json={},
    )
    assert unknown.status_code == 400


def test_calendar_and_web_summary_templates_present(monkeypatch):
    from harness.modules.calendar import server as calendar
    from harness.modules.web import server as web

    monkeypatch.delenv("CALENDAR_READ_ONLY", raising=False)
    monkeypatch.delenv("HARNESS_CALENDAR_READ_ONLY", raising=False)
    schemas = {
        item["name"]: item for item in calendar.tool_schemas()
    }
    assert schemas["calendar_create_event"]["summary_template"] == (
        "Create the event '{summary}' ({start} → {end})"
    )
    assert schemas["calendar_update_event"]["summary_template"] == (
        "Update the event '{uid}'"
    )
    assert schemas["calendar_delete_event"]["summary_template"] == (
        "Delete the event '{uid}'"
    )
    assert "summary_template" not in schemas["calendar_list_events"]
    assert "query" in web.tool_schema()["summary_template"]

    # A template placeholder that is not an actual argument renders as
    # "(not set)" on the approval card, so the two must stay in sync.
    for schema in [*schemas.values(), web.tool_schema()]:
        template = schema.get("summary_template") or ""
        placeholders = set(re.findall(r"\{([A-Za-z0-9_]+)\}", template))
        properties = set(
            (schema.get("inputSchema") or {}).get("properties") or {}
        )
        assert placeholders <= properties, (
            schema["name"], sorted(placeholders - properties)
        )


def test_v7_approvals_survive_the_v8_chain_migration(tmp_path):
    """The v8 rebuild keeps every approval row and accepts chain statuses."""
    path = tmp_path / "harness.db"
    Database(path).close()

    # Rewind approvals to the v7 shape with one historical row.
    conn = sqlite3.connect(str(path))
    conn.executescript("""
DROP TABLE approvals;
CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  tool_call_id TEXT,
  arguments TEXT NOT NULL,
  risk TEXT NOT NULL,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN
    ('pending','approved','rejected','cancelled','executed','expired')),
  created_at TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT,
  expires_at TEXT,
  notified_at TEXT,
  meta TEXT
);
CREATE INDEX idx_approvals_status ON approvals(status);
INSERT INTO approvals(id, task_id, run_id, tool, arguments, risk, status,
  created_at, meta)
VALUES ('apr_old', 'task_old', 'run_old', 'mcp_test_send',
  '{"text": "kept"}', 'confirm', 'executed', '2026-01-01T00:00:00Z', '{}');
UPDATE schema_meta SET value='7' WHERE key='schema_version';
""")
    conn.commit()
    conn.close()

    reopened = Database(path)
    try:
        version = reopened.query_one(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        )["value"]
        assert int(version) == SCHEMA_VERSION == 9
        row = reopened.query_one(
            "SELECT * FROM approvals WHERE id='apr_old'"
        )
        assert row is not None
        assert row["tool"] == "mcp_test_send"
        assert row["status"] == "executed"
        assert util.loads(row["arguments"], {}) == {"text": "kept"}
        assert row["supersedes_id"] is None
        # The extended CHECK constraint accepts the revision chain.
        reopened.execute(
            "UPDATE approvals SET status='revised' WHERE id='apr_old'"
        )
        reopened.execute(
            "UPDATE approvals SET status='superseded' WHERE id='apr_old'"
        )
        assert list((tmp_path / "backups").glob(
            f"pre-migrate-v7-to-v{SCHEMA_VERSION}-*.zip"
        ))
    finally:
        reopened.close()

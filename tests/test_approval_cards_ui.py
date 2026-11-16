"""Phase 8B UI contracts for approval cards and their HTTP actions."""

from __future__ import annotations

import json

from harness import util
from harness.tools import ToolSpec


def _register_card_tool(app, received=None, name="ui_card_send"):
    """Register a deterministic confirm-risk tool for the card UI."""

    def handler(ctx, args):
        if received is not None:
            received.append(dict(args))
        return {"done": True, "echo": args.get("text")}

    app.tools.register(ToolSpec(
        name=name,
        description="send a card message somewhere",
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
        side_effects=False,
        summary_template="Send {text}",
    ))


def _manual_run(app, task):
    run_id = util.new_id("run")
    app.db.execute(
        "INSERT INTO runs(id, task_id, trigger, status, started_at,"
        " transcript) VALUES (?,?,?,?,?,?)",
        (run_id, task["id"], "user", "waiting_approval", util.iso(),
         util.dumps([{"role": "user", "content": "go"}])),
    )
    return run_id


def test_approvals_payload_has_card_and_edit_fields(make_app, client):
    app = make_app.build(default="ok")
    _register_card_tool(app)
    task = app.tasks.create(objective="card fields", origin="user")
    run_id = _manual_run(app, task)
    app.approvals.create(
        task_id=task["id"],
        run_id=run_id,
        action="tool",
        tool="ui_card_send",
        arguments={"text": "hello", "recipient": "alice"},
        risk="confirm",
    )

    response = client(app).get("/api/approvals")
    assert response.status_code == 200
    approval = response.json()["approvals"][0]
    assert approval["summary"] == "Send hello"
    assert set(approval["schema"]["properties"]) == {"text", "recipient"}
    assert approval["editable"] is True
    assert approval["side_effects"] is False
    assert approval["read_only"] is False
    assert approval["tainted"] is False
    assert approval["supersedes_id"] is None
    assert approval["feedback"] == ""


def test_tools_payload_has_override_and_template(make_app, client):
    app = make_app.build(default="ok")
    _register_card_tool(app)
    http = client(app)

    listing = http.get("/api/tools")
    assert listing.status_code == 200
    tools = listing.json()["tools"]
    tool = next(item for item in tools if item["name"] == "ui_card_send")
    assert "risk_override" in tool
    assert tool["risk_override"] is None
    assert tool["summary_template"] == "Send {text}"

    lowered = http.post(
        "/api/tools/ui_card_send/risk", json={"risk": "low"}
    )
    assert lowered.status_code == 200
    tools = http.get("/api/tools").json()["tools"]
    tool = next(item for item in tools if item["name"] == "ui_card_send")
    assert tool["risk_override"] == "low"

    cleared = http.post(
        "/api/tools/ui_card_send/risk", json={"risk": None}
    )
    assert cleared.status_code == 200
    tools = http.get("/api/tools").json()["tools"]
    tool = next(item for item in tools if item["name"] == "ui_card_send")
    assert tool["risk_override"] is None


def test_ui_assets_wire_approval_cards(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)

    script = http.get("/assets/app.js")
    assert script.status_code == 200
    source = script.text
    assert "renderApprovalCard(" in source
    assert "'/edit'" in source
    assert "remember: true" in source
    assert "Approve all safe" in source
    assert "INPUT" in source and "TEXTAREA" in source

    style = http.get("/assets/style.css")
    assert style.status_code == 200
    assert ".approval-card" in style.text
    assert ".approval-form" in style.text
    assert ".approval-actions" in style.text

    page = http.get("/")
    assert page.status_code == 200
    assert 'data-tab="approvals"' in page.text


def test_approval_edit_and_revise_over_http(make_app, client):
    received = []
    app = make_app.build(scripts=[
        {"tool_calls": [{
            "name": "ui_card_send",
            "arguments": {"text": "original", "recipient": "alice"},
        }]},
        {"content": "Sent the edited message."},
    ])
    _register_card_tool(app, received)
    app.runtime.submit_user_message("send original")
    approval = app.approvals.pending()[0]

    edited = client(app).post(
        f"/api/approvals/{approval['id']}/edit",
        json={"arguments": {"text": "edited", "recipient": "bob"}},
    )
    assert edited.status_code == 200
    assert received == [{"text": "edited", "recipient": "bob"}]

    revise_received = []
    revise_app = make_app.build(scripts=[
        {"tool_calls": [{
            "name": "ui_card_send",
            "arguments": {"text": "original", "recipient": "alice"},
        }]},
        {"tool_calls": [{
            "name": "ui_card_send",
            "arguments": {"text": "revised", "recipient": "bob"},
        }]},
    ])
    _register_card_tool(revise_app, revise_received)
    revise_app.runtime.submit_user_message("send original")
    revise_approval = revise_app.approvals.pending()[0]

    revised = client(revise_app).post(
        f"/api/approvals/{revise_approval['id']}/revise",
        json={"feedback": "use a shorter message"},
    )
    assert revised.status_code == 200
    assert revise_app.approvals.get(revise_approval["id"])["status"] == (
        "revised"
    )
    assert not revise_received

    row = revise_app.db.query_one(
        "SELECT transcript FROM runs WHERE id=?", (revise_approval["run_id"],)
    )
    transcript = util.loads(row["transcript"] or "[]", [])
    revision_results = [
        json.loads(message.get("content") or "{}")
        for message in transcript
        if message.get("role") == "tool"
        and "revision_requested" in message.get("content", "")
    ]
    assert len(revision_results) == 1
    assert revision_results[0]["feedback"] == "use a shorter message"

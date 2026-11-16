"""Repeated approvals are denied after the user rejects the same action."""

from __future__ import annotations

import hashlib

from harness import util


ASK_USER = "<" + "ask_user" + "/>"


def _register_confirm_tool(app):
    from harness.tools import ToolSpec

    def handler(ctx, args):
        return {"done": True, "echo": args.get("text")}

    app.tools.register(ToolSpec(
        name="mcp_test_send",
        description="send something somewhere (external effect)",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        risk="confirm",
        handler=handler,
    ))


def _make_confirm_tool_app(make_app, scripts):
    app = make_app.build(scripts=scripts)
    _register_confirm_tool(app)
    return app


def _proposal(text):
    return {
        "tool_calls": [{
            "name": "mcp_test_send",
            "arguments": {"text": text},
        }],
    }


def _args_hash(arguments):
    encoded = util.dumps(arguments).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _approval_rows(app):
    return app.db.query("SELECT * FROM approvals ORDER BY created_at, rowid")


def _matching_notes(app):
    prefix = "I want to run **mcp_test_send**"
    rows = app.db.query(
        "SELECT * FROM messages WHERE role='note' ORDER BY id"
    )
    return [row for row in rows if row["content"].startswith(prefix)]


def _run_meta(app, run_id):
    row = app.db.query_one(
        "SELECT meta FROM runs WHERE id=?", (run_id,)
    )
    return util.loads(row["meta"] or "{}", {})


def test_reject_via_approvals_button_does_not_duplicate_request(make_app):
    app = _make_confirm_tool_app(make_app, scripts=[
        _proposal("hello"),
        _proposal("hello"),
        {"content": "Understood; I will not send it."},
    ])

    first = app.runtime.submit_user_message("send hello")
    assert first.status == "waiting_approval"
    approval = app.approvals.pending()[0]

    result = app.runtime.decide_approval(approval["id"], "reject")
    assert result.status == "completed"
    assert result.message == "Understood; I will not send it."

    approvals = _approval_rows(app)
    assert len(approvals) == 1
    assert approvals[0]["id"] == approval["id"]
    assert approvals[0]["status"] == "rejected"
    assert len(_matching_notes(app)) == 1

    executions = app.db.query(
        "SELECT * FROM tool_executions WHERE tool='mcp_test_send'"
    )
    assert len(executions) == 1
    assert executions[0]["status"] == "denied"

    run = app.db.query_one(
        "SELECT * FROM runs WHERE id=?", (result.run_id,)
    )
    assert run["status"] == "completed"
    event = app.db.query_one(
        "SELECT * FROM event_log WHERE type='approval.repeat_denied'"
    )
    assert event is not None
    assert event["level"] == "warn"


def test_reject_via_chat_quick_no_does_not_duplicate_request(make_app):
    app = _make_confirm_tool_app(make_app, scripts=[
        _proposal("hello"),
        _proposal("hello"),
        {"content": "Understood; I will not send it."},
    ])

    first = app.runtime.submit_user_message("send hello")
    assert first.status == "waiting_approval"

    result = app.runtime.submit_user_message("no")
    assert result.status == "completed"
    assert result.message == "Understood; I will not send it."

    approvals = _approval_rows(app)
    assert len(approvals) == 1
    assert approvals[0]["status"] == "rejected"
    assert len(_matching_notes(app)) == 1

    executions = app.db.query(
        "SELECT * FROM tool_executions WHERE tool='mcp_test_send'"
    )
    assert len(executions) == 1
    assert executions[0]["status"] == "denied"
    assert app.db.query_one(
        "SELECT * FROM event_log WHERE type='approval.repeat_denied'"
    ) is not None


def test_different_arguments_after_reject_may_ask_again(make_app):
    app = _make_confirm_tool_app(make_app, scripts=[
        _proposal("hello"),
        _proposal("goodbye"),
    ])

    app.runtime.submit_user_message("send hello")
    first = app.approvals.pending()[0]

    result = app.runtime.decide_approval(first["id"], "reject")
    assert result.status == "waiting_approval"
    pending = app.approvals.pending()
    assert len(pending) == 1
    assert pending[0]["id"] != first["id"]
    assert pending[0]["arguments"] == {"text": "goodbye"}
    assert app.approvals.get(first["id"])["status"] == "rejected"


def test_new_user_message_clears_denial_memory(make_app):
    app = _make_confirm_tool_app(make_app, scripts=[
        _proposal("hello"),
        {"content": f"Should I still send it? {ASK_USER}"},
        _proposal("hello"),
    ])

    app.runtime.submit_user_message("send hello")
    first = app.approvals.pending()[0]
    rejected = app.runtime.decide_approval(first["id"], "reject")
    assert rejected.status == "waiting_user"

    result = app.runtime.submit_user_message("please send hello again")
    assert result.status == "waiting_approval"
    assert result.run_id == rejected.run_id
    pending = app.approvals.pending()
    assert len(pending) == 1
    assert pending[0]["id"] != first["id"]
    assert pending[0]["arguments"] == {"text": "hello"}

    meta = _run_meta(app, result.run_id)
    denied = meta.get("denied_actions", [])
    assert not any(
        entry.get("tool") == "mcp_test_send"
        and entry.get("args_hash") == _args_hash({"text": "hello"})
        for entry in denied
    )


def test_denial_memory_is_bounded(make_app):
    texts = [f"message-{index}" for index in range(21)]
    scripts = [_proposal(text) for text in texts]
    scripts.append({"content": "All requested actions were rejected."})
    app = _make_confirm_tool_app(make_app, scripts=scripts)
    app.config.set("limits.max_iterations", 64)
    app.config.set("limits.max_tool_calls", 64)

    app.runtime.submit_user_message("send the first message")
    for index in range(len(texts)):
        pending = app.approvals.pending()
        assert len(pending) == 1
        assert pending[0]["arguments"] == {"text": texts[index]}
        result = app.runtime.decide_approval(pending[0]["id"], "reject")
        if index < len(texts) - 1:
            assert result.status == "waiting_approval"
        else:
            assert result.status == "completed"

    meta = _run_meta(app, result.run_id)
    denied = meta.get("denied_actions", [])
    assert len(denied) <= 20
    newest_hash = _args_hash({"text": texts[-1]})
    assert any(
        entry.get("tool") == "mcp_test_send"
        and entry.get("args_hash") == newest_hash
        for entry in denied
    )

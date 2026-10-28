"""Tools, MCP integration, and permission plumbing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _mcp_config(tmp_path, server_file: Path, autoapprove: bool) -> Path:
    import yaml

    config_path = tmp_path / "mcp.yaml"
    config_path.write_text(yaml.safe_dump({
        "servers": [{
            "name": "test_echo",
            "command": sys.executable,
            "args": [str(server_file)],
            "autoapprove": autoapprove,
        }]
    }), encoding="utf-8")
    return config_path


def _echo_server(tmp_path: Path) -> Path:
    server_file = tmp_path / "echo_server.py"
    server_file.write_text((REPO / "examples" / "mcp" / "echo_server.py")
                           .read_text(encoding="utf-8"), encoding="utf-8")
    return server_file


def test_mcp_tools_discovered_and_risk_applied(make_app, tmp_path):
    app = make_app.build(default="ok")
    app.config.set("tools.mcp_servers_file",
                   str(_mcp_config(tmp_path, _echo_server(tmp_path),
                                   autoapprove=False)))
    from harness.tools import McpTools

    mcp = McpTools(app.tools, app.config, app.events)
    mcp.load()
    names = app.tools.names()
    assert "mcp_test_echo_echo" in names
    assert app.tools._tools["mcp_test_echo_echo"].risk == "confirm"
    status = mcp.status.get("test_echo", "")
    assert status.startswith("ok (")


def test_mcp_autoapprove_marks_low_risk(make_app, tmp_path):
    app = make_app.build(default="ok")
    app.config.set("tools.mcp_servers_file",
                   str(_mcp_config(tmp_path, _echo_server(tmp_path),
                                   autoapprove=True)))
    from harness.tools import McpTools

    mcp = McpTools(app.tools, app.config, app.events)
    mcp.load()
    assert app.tools._tools["mcp_test_echo_echo"].risk == "low"


def test_mcp_call_round_trip(make_app, tmp_path):
    app = make_app.build(default="ok")
    app.config.set("tools.mcp_servers_file",
                   str(_mcp_config(tmp_path, _echo_server(tmp_path),
                                   autoapprove=True)))
    from harness.tools import McpTools

    mcp = McpTools(app.tools, app.config, app.events)
    mcp.load()
    ctx = app.tool_context("run_x", "tsk_x", None)
    from harness.model import ToolCall

    call = ToolCall(id="c1", name="mcp_test_echo_echo",
                    arguments={"text": "round trip"},
                    raw_arguments=json.dumps({"text": "round trip"}))
    outcome = app.tools.execute(call, ctx, run_id="run_x", task_id="tsk_x")
    assert outcome["status"] == "succeeded"
    assert "round trip" in json.dumps(outcome["result"])


def test_mcp_tool_result_is_bounded(make_app, tmp_path):
    app = make_app.build(default="ok")
    app.config.set("tools.mcp_servers_file",
                   str(_mcp_config(tmp_path, _echo_server(tmp_path),
                                   autoapprove=True)))
    from harness.tools import McpTools

    mcp = McpTools(app.tools, app.config, app.events)
    mcp.load()
    ctx = app.tool_context("run_x", "tsk_x", None)
    from harness.model import ToolCall

    huge = "x" * 100000
    call = ToolCall(id="c1", name="mcp_test_echo_echo",
                    arguments={"text": huge},
                    raw_arguments=json.dumps({"text": huge}))
    app.config.set("limits.max_tool_result_chars", 300)
    outcome = app.tools.execute(call, ctx, run_id="run_x", task_id="tsk_x")
    assert outcome["status"] == "succeeded"
    row = app.db.query_one(
        "SELECT result FROM tool_executions WHERE tool='mcp_test_echo_echo'"
    )
    assert len(row["result"] or "") < 400


def test_missing_mcp_config_is_unavailable_not_fatal(make_app, tmp_path):
    app = make_app.build(default="ok")
    app.config.set("tools.mcp_servers_file",
                   str(tmp_path / "nope.yaml"))
    from harness.tools import McpTools

    mcp = McpTools(app.tools, app.config, app.events)
    mcp.load()  # must not raise
    events = [e["type"] for e in app.events.list()]
    assert "mcp.config_missing" in events


def test_broken_mcp_server_is_unavailable_not_fatal(make_app, tmp_path):
    app = make_app.build(default="ok")
    bad = tmp_path / "bad_server.py"
    bad.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    import yaml

    config_path = tmp_path / "mcp.yaml"
    config_path.write_text(yaml.safe_dump({
        "servers": [{"name": "broken", "command": sys.executable,
                     "args": [str(bad)], "autoapprove": True}]
    }), encoding="utf-8")
    app.config.set("tools.mcp_servers_file", str(config_path))
    from harness.tools import McpTools

    mcp = McpTools(app.tools, app.config, app.events)
    mcp.load()  # must not raise
    assert "broken" in mcp.status and "unavailable" in mcp.status["broken"]


def test_skills_disclosed_by_preconditions(make_app, tmp_path):
    app = make_app.build(default="ok")
    skills_dir = tmp_path / "skills"
    (skills_dir / "weekly-review").mkdir(parents=True)
    (skills_dir / "weekly-review" / "SKILL.md").write_text(
        "---\nname: weekly-review\ndescription: weekly planning\n"
        "preconditions: weekly, review, planning\n---\n"
        "List open tasks first.\n",
        encoding="utf-8",
    )
    app.config.set("tools.skills_dir", str(skills_dir))
    task = app.tasks.create(objective="let us do the weekly planning review",
                            origin="user")
    built = app.context.build(task=task, domain=None, trigger={"kind": "user"})
    assert "weekly-review" in built["system"]
    # Non-matching objective: skill withheld with a recorded reason.
    task2 = app.tasks.create(objective="make coffee", origin="user")
    built2 = app.context.build(task=task2, domain=None, trigger={"kind": "user"})
    assert "weekly-review" not in built2["system"]
    reasons = [s["reason"] for s in built2["sources"]
               if s["section"] == "skills"]
    assert any("preconditions" in r for r in reasons)

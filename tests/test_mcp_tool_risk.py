"""Phase 8.1: per-tool MCP risk from trusted annotations and overrides."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from harness import util
from harness.modules.calendar import server as calendar_server
from harness.modules.web import server as web_server

REPO = Path(__file__).resolve().parents[1]

_FAKE_SERVER = r'''
"""Inline MCP server with one annotated read and one unannotated write."""

import json
import sys


TOOLS = [
    {
        "name": "read_record",
        "description": "Read one untrusted record.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "write_record",
        "description": "Write one record.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "risk-fixture", "version": "1.0"},
            },
        })
    elif method == "tools/list":
        send({
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {"tools": TOOLS},
        })
    elif method == "tools/call":
        name = str((message.get("params") or {}).get("name") or "")
        if name == "read_record":
            payload = {
                "content": "untrusted record body",
                "sources": ["https://calendar.test/events/one"],
            }
        else:
            payload = {
                "content": "record written",
                "sources": ["https://calendar.test/write"],
            }
        send({
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps(payload),
                }],
            },
        })
'''


def _fake_server(tmp_path: Path) -> Path:
    """Write and return the inline stdio MCP server fixture."""
    path = tmp_path / "risk_server.py"
    path.write_text(_FAKE_SERVER, encoding="utf-8")
    return path


def _write_config(
    path: Path,
    *,
    trust_annotations: bool | None = None,
    tool_risk: dict | None = None,
    risk_default: str = "confirm",
    shape: str = "servers",
) -> None:
    """Write a harness or standard MCP server registration file."""
    import yaml

    entry = {
        "name": "risk_server",
        "command": sys.executable,
        "args": [str(_fake_server(path.parent))],
        "tool_prefix": "",
        "risk_default": risk_default,
        "read_only": False,
        "side_effects": True,
    }
    if trust_annotations is not None:
        entry["trust_annotations"] = trust_annotations
    if tool_risk is not None:
        entry["tool_risk"] = tool_risk
    if shape == "mcpServers":
        data = {"mcpServers": {"risk_server": entry}}
    else:
        data = {"servers": [entry]}
    path.write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )


def _configure(
    app,
    tmp_path: Path,
    *,
    trust_annotations: bool | None = None,
    tool_risk: dict | None = None,
    risk_default: str = "confirm",
    shape: str = "servers",
) -> Path:
    """Configure the fixture server and reload the module registry."""
    path = tmp_path / "mcp_servers.yaml"
    _write_config(
        path,
        trust_annotations=trust_annotations,
        tool_risk=tool_risk,
        risk_default=risk_default,
        shape=shape,
    )
    app.config.set("tools.mcp_servers_file", str(path))
    app.modules.reload()
    return path


def _call_script(name: str) -> dict:
    """Return one scripted model turn that calls ``name``."""
    return {"tool_calls": [{"name": name, "arguments": {}}]}


def _spec(app, name: str):
    """Return the registered tool spec named ``name``."""
    return app.tools.resolve(name)


def test_trusted_annotations_apply_per_tool_risk(make_app, tmp_path):
    app = make_app.build(default="ok")
    _configure(app, tmp_path, trust_annotations=True)

    read_spec = _spec(app, "read_record")
    assert read_spec.risk == "low"
    assert read_spec.read_only is True
    assert read_spec.side_effects is False
    assert read_spec.untrusted_output is True

    write_spec = _spec(app, "write_record")
    assert write_spec.risk == "confirm"
    assert write_spec.read_only is False
    assert write_spec.side_effects is True
    assert write_spec.untrusted_output is True


def test_trusted_annotated_read_runs_without_approval(make_app, tmp_path):
    app = make_app.build(
        scripts=[_call_script("read_record")], default="read complete"
    )
    _configure(app, tmp_path, trust_annotations=True)

    result = app.runtime.submit_user_message("read the record")

    assert result.status == "completed"
    assert app.approvals.pending() == []


def test_unannotated_write_waits_for_approval(make_app, tmp_path):
    app = make_app.build(
        scripts=[_call_script("write_record")], default="write complete"
    )
    _configure(app, tmp_path, trust_annotations=True)

    result = app.runtime.submit_user_message("write the record")

    assert result.status == "waiting_approval"
    assert len(app.approvals.pending()) == 1


def test_untrusted_annotations_do_not_downgrade_read_tool(
    make_app, tmp_path
):
    app = make_app.build(default="ok")
    _configure(app, tmp_path)

    spec = _spec(app, "read_record")
    assert spec.risk == "confirm"
    assert spec.read_only is False
    assert spec.side_effects is True


def test_tool_risk_can_force_annotated_read_to_confirm(make_app, tmp_path):
    app = make_app.build(
        scripts=[_call_script("read_record")], default="read complete"
    )
    _configure(
        app,
        tmp_path,
        trust_annotations=True,
        tool_risk={"read_record": "confirm"},
    )

    result = app.runtime.submit_user_message("read the record")

    assert result.status == "waiting_approval"
    assert len(app.approvals.pending()) == 1


def test_tool_risk_can_force_unannotated_write_to_low(make_app, tmp_path):
    app = make_app.build(
        scripts=[_call_script("write_record")], default="write complete"
    )
    _configure(
        app,
        tmp_path,
        trust_annotations=True,
        tool_risk={"write_record": "low"},
    )

    result = app.runtime.submit_user_message("write the record")

    assert result.status == "completed"
    assert app.approvals.pending() == []
    spec = _spec(app, "write_record")
    assert spec.risk == "low"
    assert spec.read_only is False
    assert spec.side_effects is True


def test_annotated_read_taints_run_and_escalates_following_write(
    make_app, tmp_path
):
    app = make_app.build(scripts=[
        _call_script("read_record"),
        _call_script("write_record"),
    ], default="done")
    _configure(app, tmp_path, trust_annotations=True, risk_default="low")

    result = app.runtime.submit_user_message("read then write")

    assert result.status == "waiting_approval"
    run = app.db.query_one(
        "SELECT meta FROM runs WHERE id=?", (result.run_id,)
    )
    meta = util.loads(run["meta"], {})
    assert meta["tainted_sources"][0]["tool"] == "read_record"
    assert meta["tainted_sources"][0]["origin"] == (
        "https://calendar.test/events/one"
    )
    approval = app.approvals.pending()[0]
    assert approval["tool"] == "write_record"
    assert approval["risk"] == "confirm"
    assert "untrusted content" in approval["reason"]
    assert "https://calendar.test/events/one" in approval["reason"]


def test_manifest_import_carries_trust_and_sanitized_tool_risk(
    make_app, tmp_path
):
    app = make_app.build(default="ok")
    raw_risk = {
        "read_record": "confirm",
        "write_record": "low",
        "invalid": "unsafe",
        7: "low",
    }
    for index in range(205):
        raw_risk[f"tool_{index:03d}"] = "low"
    path = tmp_path / "mcp_servers.yaml"
    _write_config(
        path,
        trust_annotations=True,
        tool_risk=raw_risk,
        shape="mcpServers",
    )
    app.config.set("tools.mcp_servers_file", str(path))
    app.modules.reload()

    module = app.modules.get("mcp:risk_server")
    assert module is not None
    assert module.manifest["trust_annotations"] is True
    stored_risk = module.manifest["tool_risk"]
    assert stored_risk["read_record"] == "confirm"
    assert stored_risk["write_record"] == "low"
    assert "invalid" not in stored_risk
    assert 7 not in stored_risk
    assert len(stored_risk) == 200
    assert set(stored_risk.values()) <= {"low", "confirm"}


def test_reload_picks_up_new_manifest_risk_keys(make_app, tmp_path):
    app = make_app.build(default="ok")
    path = _configure(app, tmp_path)
    assert _spec(app, "read_record").risk == "confirm"

    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entry = data["servers"][0]
    entry["trust_annotations"] = True
    entry["tool_risk"] = {"read_record": "low"}
    path.write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    app.modules.reload()

    module = app.modules.get("mcp:risk_server")
    assert module is not None
    assert module.manifest["trust_annotations"] is True
    assert module.manifest["tool_risk"] == {"read_record": "low"}
    assert _spec(app, "read_record").risk == "low"


def test_server_schemas_annotate_only_the_read_tools(monkeypatch):
    monkeypatch.setattr(calendar_server, "_FILE_CONFIG", None)
    monkeypatch.setattr(calendar_server, "_CONFIG_PATH", "")
    monkeypatch.delenv("CALENDAR_READ_ONLY", raising=False)
    monkeypatch.delenv("HARNESS_CALENDAR_READ_ONLY", raising=False)

    web_schema = web_server.tool_schema()
    assert web_schema["annotations"]["readOnlyHint"] is True

    schemas = calendar_server.tool_schemas()
    annotated = {
        schema["name"] for schema in schemas if "annotations" in schema
    }
    assert annotated == {
        "calendar_list_calendars",
        "calendar_list_events",
        "calendar_get_event",
    }
    for schema in schemas:
        if schema["name"] in annotated:
            assert schema["annotations"]["readOnlyHint"] is True


def test_example_mcp_json_trusts_web_and_calendar_annotations():
    data = json.loads(
        (REPO / "examples" / "mcp.json").read_text(encoding="utf-8")
    )
    servers = data["mcpServers"]
    assert servers["web"]["trust_annotations"] is True
    assert servers["calendar"]["trust_annotations"] is True

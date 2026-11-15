"""Installing MCP servers: standard config shape, reload sync, diagnostics."""

from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

import pytest

from harness import util
from harness.config import load_config
from harness.tools import McpError, McpServer

REPO = Path(__file__).resolve().parents[1]
ECHO = REPO / "examples" / "mcp" / "echo_server.py"
WEB_SERVER = REPO / "harness" / "modules" / "web" / "server.py"


def _write_servers(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8",
    )


def _echo_entry(**overrides) -> dict:
    entry = {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(ECHO)],
        "autoapprove": True,
        "read_only": True,
        "tools_allow": ["echo"],
    }
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_standard_mcpservers_file_is_auto_detected(make_app, data_dir):
    _write_servers(data_dir / "mcp.json", {"auto": _echo_entry()})

    app = make_app.build(default="ok")

    module = app.modules.get("mcp:auto")
    assert module is not None and module.status == "active"
    assert module.manifest["transport"] == "stdio"
    assert "mcp_auto_echo" in app.tools.names()
    assert app.modules.status["mcp:auto"].startswith("ok")


def test_reload_resyncs_an_edited_entry(make_app, data_dir):
    config_file = data_dir / "mcp.json"
    _write_servers(config_file, {"auto": _echo_entry()})
    app = make_app.build(default="ok")
    assert "mcp_auto_echo" in app.tools.names()

    _write_servers(
        config_file,
        {"auto": _echo_entry(tools_allow=["add"], read_only=False)},
    )
    report = app.modules.reload()

    assert "mcp:auto" in report["loaded"]
    assert "mcp_auto_add" in app.tools.names()
    assert "mcp_auto_echo" not in app.tools.names()
    module = app.modules.get("mcp:auto")
    assert module.manifest["tools_allow"] == ["add"]
    assert module.manifest["read_only"] is False


def test_restart_picks_up_an_edited_entry(make_app, data_dir):
    config_file = data_dir / "mcp.json"
    _write_servers(config_file, {"auto": _echo_entry()})
    app = make_app.build(default="ok")
    assert "mcp_auto_echo" in app.tools.names()

    _write_servers(
        config_file,
        {"auto": _echo_entry(tools_allow=["add"], autoapprove=False)},
    )
    restarted = make_app.restart(default="ok")

    assert "mcp_auto_add" in restarted.tools.names()
    assert "mcp_auto_echo" not in restarted.tools.names()


def test_data_dir_placeholder_is_expanded(make_app, data_dir):
    _write_servers(
        data_dir / "mcp.json",
        {"auto": _echo_entry(args=[str(ECHO), "${data_dir}/server.json"])},
    )

    app = make_app.build(default="ok")
    module = app.modules.get("mcp:auto")
    assert module.manifest["args"][-1] == "${data_dir}/server.json"

    server = app.modules.loader._server(module)
    assert Path(server.args[-1]) == (data_dir / "server.json").resolve()


def test_env_values_live_in_a_private_sidecar_not_the_module_row(
    make_app, data_dir, monkeypatch
):
    monkeypatch.delenv("MCP_TEST_TOKEN", raising=False)
    _write_servers(
        data_dir / "mcp.json",
        {"auto": _echo_entry(env={"MCP_TEST_TOKEN": "secret-value"})},
    )

    app = make_app.build(default="ok")
    module = app.modules.get("mcp:auto")

    assert module.manifest["env_names"] == ["MCP_TEST_TOKEN"]
    assert "secret-value" not in util.dumps(module.manifest)
    sidecar = app.modules.root / "mcp" / "auto.env.json"
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {
        "MCP_TEST_TOKEN": "secret-value",
    }
    assert app.modules.loader._server(module).env["MCP_TEST_TOKEN"] == "secret-value"


def test_legacy_capabilities_paths_are_migrated(tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "capabilities:\n  mcp_servers_file: mcp.json\n  skills_dir: skills\n",
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.get("tools.mcp_servers_file") == "mcp.json"
    assert config.get("tools.skills_dir") == "skills"


# ---------------------------------------------------------------------------
# Diagnostics and shipped examples
# ---------------------------------------------------------------------------

def test_failed_server_reports_its_stderr(tmp_path):
    broken = tmp_path / "broken.py"
    broken.write_text(
        "import sys\n"
        "print('boom: missing credentials', file=sys.stderr)\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )

    with pytest.raises(McpError) as exc:
        McpServer("broken", sys.executable, [str(broken)]).list_tools()

    assert "boom: missing credentials" in str(exc.value)


def test_web_server_validates_its_config_at_discovery(tmp_path):
    config_file = tmp_path / "web.json"
    config_file.write_text(
        json.dumps({"searxng_url": "not-a-url"}), encoding="utf-8",
    )

    with pytest.raises(McpError) as exc:
        McpServer(
            "web", sys.executable,
            [str(WEB_SERVER), "--config", str(config_file)],
        ).list_tools()

    assert "http(s) URL" in str(exc.value)


def test_shipped_example_configs_are_valid():
    web = json.loads(
        (REPO / "harness" / "modules" / "web" / "web.example.json")
        .read_text(encoding="utf-8")
    )
    parts = urllib.parse.urlsplit(web["searxng_url"])
    assert parts.scheme in ("http", "https") and parts.netloc

    calendar = json.loads(
        (REPO / "harness" / "modules" / "calendar" / "calendar.example.json")
        .read_text(encoding="utf-8")
    )
    assert calendar["url"].startswith("http")

    standard = json.loads(
        (REPO / "examples" / "mcp.json").read_text(encoding="utf-8")
    )
    assert "example_echo" in standard["mcpServers"]

"""Phase 12B: authenticated MCP setup, preview, templates, and tool risk."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
ECHO = str(REPO / "examples" / "mcp" / "echo_server.py")


def _echo_entry(name: str = "echo_setup") -> dict:
    return {
        "name": name,
        "command": sys.executable,
        "args": [ECHO],
        "tool_prefix": "",
        "risk_default": "low",
        "autoapprove": True,
        "read_only": True,
        "side_effects": False,
    }


def test_create_patch_delete_registers_and_removes_mcp_tools(
    make_app, client
):
    app = make_app.build(default="ok")
    api = client(app)
    entry = _echo_entry()

    rejected = api.post("/api/modules/mcp", json={**entry, "confirm": False})
    assert rejected.status_code == 400
    assert "confirmation" in rejected.json()["error"]
    assert app.modules.get("mcp:echo_setup") is None

    created = api.post("/api/modules/mcp", json={**entry, "confirm": True})
    assert created.status_code == 200
    module = created.json()["module"]
    assert module["id"] == "mcp:echo_setup"
    assert module["editable"] is True
    assert {tool["name"] for tool in module["tools"]} == {"echo", "add"}
    assert "echo" in app.tools.names()
    assert "module.created" in {
        event["type"] for event in app.events.list()
    }

    updated = api.patch(
        "/api/modules/mcp:echo_setup",
        json={"tools_allow": ["echo"]},
    )
    assert updated.status_code == 200
    assert updated.json()["module"]["manifest"]["tools_allow"] == ["echo"]
    assert {
        tool.tool_name for tool in app.modules.tools("mcp:echo_setup")
    } == {"echo"}
    assert "echo" in app.tools.names()
    assert "add" not in app.tools.names()

    deleted = api.delete("/api/modules/mcp:echo_setup")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": "mcp:echo_setup"}
    assert app.modules.get("mcp:echo_setup") is None
    assert "echo" not in app.tools.names()


def test_command_allowlist_requires_an_explicit_override(make_app, client):
    app = make_app.build(default="ok")
    api = client(app)
    entry = {
        "name": "not_real",
        "command": "definitely-not-a-real-command",
        "args": [],
        "confirm": True,
    }

    rejected = api.post("/api/modules/mcp", json=entry)
    assert rejected.status_code == 400
    assert "allowlist" in rejected.json()["error"]
    assert "allow_any_command" in rejected.json()["error"]
    assert app.modules.get("mcp:not_real") is None

    allowed = api.post(
        "/api/modules/mcp", json={**entry, "allow_any_command": True}
    )
    assert allowed.status_code == 200
    module = allowed.json()["module"]
    assert module["id"] == "mcp:not_real"
    assert module["provenance"]["command_override"] is True
    # The row remains available for correction even though the command fails.
    assert module["last_error"]


def test_connection_preview_lists_tools_and_reports_stderr(
    make_app, client, tmp_path
):
    app = make_app.build(default="ok")
    api = client(app)
    before = len(app.modules.all())

    preview = api.post("/api/modules/test", json={
        "command": sys.executable,
        "args": [ECHO],
    })
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["ok"] is True
    assert {tool["name"] for tool in payload["tools"]} == {"echo", "add"}
    assert payload["count"] == 2
    assert len(app.modules.all()) == before

    broken = tmp_path / "broken_server.py"
    broken.write_text(
        "import sys\n"
        "print('broken-preview-marker', file=sys.stderr)\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    failed = api.post("/api/modules/test", json={
        "command": sys.executable,
        "args": [str(broken)],
    })
    assert failed.status_code == 200
    result = failed.json()
    assert result["ok"] is False
    assert result["tools"] == []
    assert "broken-preview-marker" in result["stderr_tail"]


def test_env_values_stay_in_the_private_sidecar(make_app, client):
    app = make_app.build(default="ok")
    api = client(app)
    secret = "mcp-secret-value"
    entry = {
        **_echo_entry("secret_env"),
        "env": {"API_TOKEN": secret},
    }

    created = api.post("/api/modules/mcp", json={**entry, "confirm": True})
    assert created.status_code == 200
    module = created.json()["module"]
    assert module["env_names"] == ["API_TOKEN"]
    assert module["env_set"] == ["API_TOKEN"]
    assert secret not in created.text

    sidecar = app.data_dir / "modules" / "mcp" / "secret_env.env.json"
    assert sidecar.is_file()
    assert secret in sidecar.read_text(encoding="utf-8")

    listed = api.get("/api/modules")
    assert listed.status_code == 200
    assert secret not in listed.text
    listed_module = next(
        item for item in listed.json()["modules"]
        if item["id"] == "mcp:secret_env"
    )
    assert listed_module["env_names"] == ["API_TOKEN"]
    assert listed_module["env_set"] == ["API_TOKEN"]

    # An unrelated edit preserves the omitted private value.
    preserved = api.patch(
        "/api/modules/mcp:secret_env", json={"tools_allow": ["echo"]}
    )
    assert preserved.status_code == 200
    assert secret in sidecar.read_text(encoding="utf-8")

    removed = api.patch(
        "/api/modules/mcp:secret_env", json={"env": {"API_TOKEN": None}}
    )
    assert removed.status_code == 200
    assert removed.json()["module"]["env_names"] == []
    assert removed.json()["module"]["env_set"] == []
    assert not sidecar.exists()


def test_per_tool_risk_updates_the_registered_spec(make_app, client):
    app = make_app.build(default="ok")
    api = client(app)
    created = api.post(
        "/api/modules/mcp",
        json={**_echo_entry("risk_setup"), "confirm": True},
    )
    assert created.status_code == 200
    assert app.tools.resolve("echo").risk == "low"

    changed = api.post(
        "/api/modules/mcp:risk_setup/tool-risk",
        json={"tool": "echo", "risk": "confirm"},
    )
    assert changed.status_code == 200
    module = changed.json()["module"]
    assert module["manifest"]["tool_risk"] == {"echo": "confirm"}
    assert module["tool_risk"] == {"echo": "confirm"}
    assert app.tools.resolve("echo").risk == "confirm"
    assert "module.tool_risk" in {
        event["type"] for event in app.events.list()
    }

    cleared = api.post(
        "/api/modules/mcp:risk_setup/tool-risk",
        json={"tool": "echo", "risk": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["module"]["manifest"]["tool_risk"] == {}
    assert app.tools.resolve("echo").risk == "low"


def test_bundled_templates_install_private_config_and_tools(
    make_app, client
):
    app = make_app.build(default="ok")
    api = client(app)

    templates = api.get("/api/modules/templates")
    assert templates.status_code == 200
    by_id = {
        template["id"]: template
        for template in templates.json()["templates"]
    }
    assert {"web", "calendar"} <= set(by_id)
    assert by_id["web"]["fields"]
    assert by_id["calendar"]["fields"]
    password_field = next(
        field for field in by_id["calendar"]["fields"]
        if field["name"] == "password"
    )
    assert password_field["type"] == "secret"

    installed = api.post("/api/modules/templates/web/install", json={
        "values": {"searxng_url": "http://127.0.0.1:9999"},
        "confirm": True,
    })
    assert installed.status_code == 200
    payload = installed.json()
    assert payload["module"]["id"] == "mcp:web"
    assert Path(payload["config_path"]) == (
        app.data_dir / "modules" / "mcp" / "web.json"
    )
    assert (app.data_dir / "modules" / "mcp" / "web.json").is_file()
    assert "web_search" in app.tools.names()
    assert "module.template_installed" in {
        event["type"] for event in app.events.list()
    }

    password = "calendar-password-must-stay-private"
    calendar = api.post("/api/modules/templates/calendar/install", json={
        "values": {
            "url": "http://127.0.0.1:9999/dav",
            "username": "calendar-user",
            "password": password,
        },
        "confirm": True,
    })
    assert calendar.status_code == 200
    assert password not in calendar.text
    calendar_config = app.data_dir / "modules" / "mcp" / "calendar.json"
    assert password in calendar_config.read_text(encoding="utf-8")


def test_delete_refuses_an_import_owned_module_still_in_its_file(
    make_app, client, tmp_path
):
    app = make_app.build(default="ok")
    api = client(app)
    servers = tmp_path / "mcp_servers.yaml"
    servers.write_text(yaml.safe_dump({"servers": [{
        "name": "imported_echo",
        "command": sys.executable,
        "args": [ECHO],
    }]}), encoding="utf-8")
    app.config.set("tools.mcp_servers_file", str(servers))
    app.modules.reload()

    deleted = api.delete("/api/modules/mcp:imported_echo")
    assert deleted.status_code == 409
    assert "edit or remove the entry there and reload" in (
        deleted.json()["error"]
    )
    assert app.modules.get("mcp:imported_echo") is not None

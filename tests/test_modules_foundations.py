"""Module foundations: registry, schema, budget, and generic MCP loading."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import yaml
from starlette.testclient import TestClient

from harness import util
from harness.config import DEFAULTS, load_config
from harness.db import Database, SCHEMA_VERSION
from harness.modules.types import Module, ModuleBinding, ModuleTool
from harness.server import Server

REPO = Path(__file__).resolve().parents[1]

# The v6 module table: its CHECK still allows kind='web'.
V6_MODULES_SCHEMA = """
CREATE TABLE modules (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('skill','mcp','web')),
  name TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK(status IN ('draft','active','disabled','error')),
  scope TEXT NOT NULL DEFAULT 'global'
    CHECK(scope IN ('global','domain','adaptive')),
  manifest TEXT NOT NULL,
  content_path TEXT,
  origin TEXT NOT NULL,
  provenance TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_loaded_at TEXT,
  last_error TEXT,
  usage_count INTEGER NOT NULL DEFAULT 0
)
"""


def _v6_db_with_web_row(path: Path, manifest: dict) -> None:
    """Create a v6 database carrying the retired system web row."""
    db = Database(path)
    db.execute("DROP TABLE modules")
    db.execute(V6_MODULES_SCHEMA)
    now = util.iso()
    db.execute(
        "INSERT INTO modules(id, kind, name, status, scope, manifest, origin, "
        "provenance, created_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("web", "web", "Web search (SearXNG)", "active", "global",
         util.dumps(manifest), "system", util.dumps({"source": "builtin:web"}),
         "system", now, now),
    )
    db.execute("UPDATE schema_meta SET value='6' WHERE key='schema_version'")
    db.close()


def _install_echo(app, tmp_path: Path, name: str = "test_echo") -> str:
    """Register the echo MCP server through the generic import file."""
    config_path = tmp_path / "mcp.yaml"
    config_path.write_text(yaml.safe_dump({"servers": [{
        "name": name,
        "command": sys.executable,
        "args": [str(REPO / "examples" / "mcp" / "echo_server.py")],
        "autoapprove": True,
        "read_only": True,
        "tools_allow": ["echo", "add"],
    }]}), encoding="utf-8")
    app.config.set("tools.mcp_servers_file", str(config_path))
    app.modules.reload()
    return f"mcp:{name}"


def test_module_registry_is_the_authoritative_capability_loader(
    make_app, tmp_path
):
    app = make_app.build(default="ok")
    module_id = _install_echo(app, tmp_path)
    report = app.modules.load_all()
    assert module_id in report["loaded"]
    assert report["errors"] == []
    assert app.modules.root.is_dir()
    assert app.config.get("modules.enabled") is True
    assert app.config.get("modules.dir") == "modules"
    assert app.config.get("modules.health_interval_s") == 300.0
    assert app.config.get("modules.tool_schema_budget_tokens") == 4096
    assert "mcp_test_echo_echo" in app.tools.names()
    module = app.modules.get(module_id)
    assert module is not None and module.kind == "mcp"
    assert module.status == "active"
    tables = {
        row["name"] for row in app.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"modules", "module_bindings", "module_tools"} <= tables


def test_retired_web_row_migrates_to_a_generic_mcp_module(make_app):
    app = make_app.build(default="ok")
    server = REPO / "harness" / "modules" / "web" / "server.py"
    now = util.iso()
    app.modules.save(Module(
        id="web",
        kind="mcp",
        name="Web search (SearXNG)",
        status="active",
        scope="global",
        manifest={
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(server)],
            "env_names": ["HARNESS_WEB_SEARXNG_URL"],
            "autoapprove": True,
            "risk_default": "low",
            "tools_allow": ["web_search"],
            "read_only": True,
            "side_effects": False,
            "startup": "lazy",
            "timeout_s": 30.0,
        },
        origin="system",
        provenance={"source": "builtin:web"},
        created_by="system",
        created_at=now,
        updated_at=now,
    ))
    app.config.removed["web"] = {
        "enabled": True,
        "searxng_url": "http://127.0.0.1:9",
        "max_results": 5,
    }

    app.modules.reload()

    assert app.modules.get("web") is None
    migrated = app.modules.get("mcp:web")
    assert migrated is not None and migrated.kind == "mcp"
    assert migrated.manifest["tool_prefix"] == ""
    assert "--config" in migrated.manifest["args"]
    config_file = app.modules.root / "mcp" / "web.json"
    assert config_file.is_file()
    assert "searxng_url" in config_file.read_text(encoding="utf-8")
    assert migrated.manifest["env_names"] == ["HARNESS_WEB_SEARXNG_URL"]
    assert "web_search" in app.tools.names()
    assert app.tools.resolve("web_search").module == "mcp:web"
    assert "module.migrated" in [e["type"] for e in app.events.list()]


def test_retired_web_config_group_is_kept_for_migration_only(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    group = {"enabled": True, "searxng_url": "http://127.0.0.1:9"}
    (data_dir / "config.yaml").write_text(
        yaml.safe_dump({"timezone": "UTC", "web": group}),
        encoding="utf-8",
    )

    config = load_config(data_dir)

    assert config.removed["web"] == group
    assert "web" not in config.as_dict()
    assert config.get("web") is None
    assert config.get("timezone") == "UTC"
    # The file keeps the group until the module loader consumes it.
    on_disk = yaml.safe_load(
        (data_dir / "config.yaml").read_text(encoding="utf-8")
    )
    assert on_disk["web"] == group


def test_registry_round_trips_records_and_bindings(make_app):
    app = make_app.build(default="ok")
    module = Module(
        id="skill_demo",
        kind="skill",
        name="Demo",
        status="active",
        scope="domain",
        manifest={"preconditions": ["demo"]},
        content_path="skills/skill_demo/v1.md",
        origin="import",
        provenance={"source": "test"},
        created_by="test",
    )
    saved = app.modules.save(module)
    assert saved.id == "skill_demo"
    assert saved.created_at and saved.updated_at
    assert app.modules.get("skill_demo").manifest == {"preconditions": ["demo"]}

    app.modules.set_binding(ModuleBinding(
        module_id="skill_demo", domain_id="dom_a", enabled=True,
    ))
    assert app.modules.bindings("skill_demo")[0].enabled is True

    app.modules.replace_tools("skill_demo", [ModuleTool(
        module_id="skill_demo", tool_name="demo_tool",
        schema={"type": "object", "properties": {}},
        risk="low", side_effects=False, read_only=True,
        last_seen=util.iso(),
    )])
    tools = app.modules.tools("skill_demo")
    assert [tool.tool_name for tool in tools] == ["demo_tool"]
    assert tools[0].read_only is True
    assert app.modules.get("skill_demo").name == "Demo"


def test_schema_migration_from_v2_opens_an_existing_database(tmp_path):
    path = tmp_path / "harness.db"
    db = Database(path)
    # Emulate a v2 database: no module tables, version 2, existing state.
    db.execute("DROP TABLE module_tools")
    db.execute("DROP TABLE module_bindings")
    db.execute("DROP TABLE modules")
    db.execute("UPDATE schema_meta SET value='2' WHERE key='schema_version'")
    db.meta_set("sentinel", "kept")
    db.close()

    reopened = Database(path)
    try:
        version = reopened.query_one(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        )["value"]
        assert int(version) == SCHEMA_VERSION == 9
        assert reopened.meta_get("sentinel") == "kept"
        tables = {
            row["name"] for row in reopened.query(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"modules", "module_bindings", "module_tools"} <= tables
    finally:
        reopened.close()


def test_v7_migration_converts_legacy_web_kind_rows(tmp_path):
    path = tmp_path / "harness.db"
    _v6_db_with_web_row(path, {"command": "python"})

    reopened = Database(path)
    try:
        row = reopened.query_one(
            "SELECT kind, manifest FROM modules WHERE id='web'"
        )
        assert row["kind"] == "mcp"
        assert util.loads(row["manifest"]) == {"command": "python"}
    finally:
        reopened.close()


def test_v6_web_row_and_config_migrate_on_startup(make_app, data_dir):
    data_dir.mkdir(parents=True)
    (data_dir / "config.yaml").write_text(
        yaml.safe_dump({
            "timezone": "UTC",
            "web": {
                "enabled": True,
                "searxng_url": "http://127.0.0.1:9",
                "max_results": 5,
            },
        }),
        encoding="utf-8",
    )
    _v6_db_with_web_row(data_dir / "harness.db", {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(REPO / "harness" / "modules" / "web" / "server.py")],
        "env_names": ["HARNESS_WEB_SEARXNG_URL"],
        "autoapprove": True,
        "risk_default": "low",
        "tools_allow": ["web_search"],
        "read_only": True,
        "side_effects": False,
        "startup": "lazy",
        "timeout_s": 30.0,
    })

    app = make_app.build(default="ok")

    assert app.modules.get("web") is None
    module = app.modules.get("mcp:web")
    assert module is not None and module.status == "active"
    assert module.manifest["tool_prefix"] == ""
    assert module.manifest["env_names"] == ["HARNESS_WEB_SEARXNG_URL"]
    assert "web_search" in app.tools.names()
    config_file = app.modules.root / "mcp" / "web.json"
    assert "searxng_url" in config_file.read_text(encoding="utf-8")
    assert "web" not in app.config.as_dict()


def test_backup_and_restore_include_modules(make_app, tmp_path):
    app = make_app.build(default="ok")
    module_file = app.data_dir / "modules" / "skills" / "demo" / "v1.md"
    module_file.parent.mkdir(parents=True, exist_ok=True)
    module_file.write_text("durable procedure", encoding="utf-8")

    archive = app.backup(tmp_path / "backup.zip")
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert "modules/skills/demo/v1.md" in names

    stale = app.data_dir / "modules" / "stale.md"
    stale.write_text("remove me", encoding="utf-8")
    module_file.unlink()
    app.restore(archive)

    restored = app.data_dir / "modules" / "skills" / "demo" / "v1.md"
    assert restored.read_text(encoding="utf-8") == "durable procedure"
    assert not stale.exists()


def test_schema_budget_drops_whole_tools_and_records_exclusions(
    make_app, tmp_path
):
    app = make_app.build(default="ok")
    module_id = _install_echo(app, tmp_path)
    selection = app.tools.schemas_for(None, budget_tokens=1)
    assert selection["schemas"] == []
    assert selection["names"] == []
    assert len(selection["exclusions"]) == len(app.tools.specs())
    assert selection["tokens"] == 0
    for exclusion in selection["exclusions"]:
        assert exclusion["reason"] == "tool_schema_budget"
        assert exclusion["tokens"] >= 1
        assert exclusion["module"] in ("builtin", module_id)


def test_run_persists_a_global_toolset_snapshot(make_app):
    app = make_app.build(default="ok")
    app.config.set("modules.tool_schema_budget_tokens", 100000)
    result = app.runtime.submit_user_message("hello")
    assert result.status == "completed"
    run = app.db.query_one(
        "SELECT meta FROM runs WHERE id=?", (result.run_id,)
    )
    snapshot = util.loads(run["meta"], {})["toolset"]
    assert set(snapshot["names"]) == set(app.tools.names())
    assert set(snapshot["modules"]) == {
        spec.module for spec in app.tools.specs()
    }
    assert snapshot["exclusions"] == []
    assert snapshot["budget_tokens"] == 100000


def test_budget_dropped_tools_are_recorded_on_the_run(make_app):
    app = make_app.build(default="ok")
    app.config.set("modules.tool_schema_budget_tokens", 1)
    result = app.runtime.submit_user_message("hello")
    assert result.status == "completed"
    run = app.db.query_one(
        "SELECT meta, context_sources FROM runs WHERE id=?", (result.run_id,)
    )
    snapshot = util.loads(run["meta"], {})["toolset"]
    assert snapshot["names"] == []
    assert snapshot["exclusions"]
    sources = util.loads(run["context_sources"], [])
    assert any(
        source["section"] == "tools"
        and source["reason"].startswith("excluded:")
        for source in sources
    )


def test_tool_specs_drive_the_api_without_private_registry_access(
    make_app, client, tmp_path
):
    app = make_app.build(default="ok")
    module_id = _install_echo(app, tmp_path)
    specs = app.tools.specs()
    assert {spec.name for spec in specs} == set(app.tools.names())
    payload = client(app).get("/api/tools").json()
    assert {tool["name"] for tool in payload["tools"]} == set(app.tools.names())
    builtins = {tool["name"] for tool in payload["tools"]
                if tool["module"] == "builtin"}
    assert {"remember", "search_knowledge"} <= builtins
    assert any(tool["module"] == module_id for tool in payload["tools"])


def test_modules_api_lists_reloads_and_toggles(make_app, client, tmp_path):
    app = make_app.build(default="ok")
    module_id = _install_echo(app, tmp_path)
    api = client(app)

    payload = api.get("/api/modules").json()
    module = next(item for item in payload["modules"]
                  if item["id"] == module_id)
    assert module["status"] == "active"
    assert {tool["name"] for tool in module["tools"]} == {"add", "echo"}
    assert module["bound_domains"] == []
    assert payload["schema_budget_tokens"] == 4096

    disabled = api.post(f"/api/modules/{module_id}/disable").json()
    assert disabled["module"]["status"] == "disabled"
    assert "mcp_test_echo_echo" not in app.tools.names()
    assert module_id not in app.modules.eligible_module_ids(None)

    enabled = api.post(f"/api/modules/{module_id}/enable").json()
    assert enabled["module"]["status"] == "active"
    assert "mcp_test_echo_echo" in app.tools.names()

    reloaded = api.post(f"/api/modules/{module_id}/reload").json()
    assert reloaded["module"]["loaded"] is True

    report = api.post("/api/modules/reload").json()["report"]
    assert module_id in report["loaded"]


def test_heartbeat_runs_module_health_without_blocking(make_app, tmp_path):
    app = make_app.build(default="ok")
    module_id = _install_echo(app, tmp_path)
    report = app.scheduler.heartbeat()
    modules = report["modules"]
    assert modules["skipped"] is False
    assert module_id in modules["module_ids"]
    assert modules["checked"] >= 1
    assert modules["errors"] == []


def test_ui_assets_require_authentication(make_app):
    app = make_app.build(default="ok")
    unauthenticated = TestClient(Server(app).routes())
    assert unauthenticated.get("/").status_code == 401
    assert unauthenticated.get("/assets/app.js").status_code == 401
    assert unauthenticated.get("/assets/style.css").status_code == 401


def test_budget_defaults_do_not_under_reserve_the_window():
    """Schemas and the retry output budget come out of one context window.

    A schema reservation below the schema budget hides tools while claiming
    the prompt has room, and a reply reservation below the largest requested
    output budget lets a retry exceed the model window. Keep both aligned.
    """
    assert (
        DEFAULTS["context"]["tool_schema_tokens"]
        >= DEFAULTS["modules"]["tool_schema_budget_tokens"]
    )
    assert (
        DEFAULTS["context"]["reserved_output_tokens"]
        >= DEFAULTS["model"]["empty_retry_max_tokens"]
    )

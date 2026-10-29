"""Phase 0 foundations: UI shell split, module registry seam, schema v3."""

from __future__ import annotations

import zipfile

from starlette.testclient import TestClient

from harness import util
from harness.db import Database, SCHEMA_VERSION
from harness.modules.types import Module, ModuleBinding, ModuleTool
from harness.server import Server


def test_module_package_is_wired_without_changing_loaders(make_app):
    app = make_app.build(default="ok")
    assert app.modules.load_all() == {
        "loaded": [], "skipped": [], "errors": [],
    }
    assert app.modules.root.is_dir()
    assert app.config.get("modules.enabled") is True
    assert app.config.get("modules.dir") == "modules"
    assert app.config.get("modules.health_interval_s") == 300.0
    assert app.config.get("modules.tool_schema_budget_tokens") == 2400
    tables = {
        row["name"] for row in app.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"modules", "module_bindings", "module_tools"} <= tables


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
    assert app.modules.all()[0].name == "Demo"


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
        assert int(version) == SCHEMA_VERSION == 3
        assert reopened.meta_get("sentinel") == "kept"
        tables = {
            row["name"] for row in reopened.query(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"modules", "module_bindings", "module_tools"} <= tables
    finally:
        reopened.close()


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


def test_schema_budget_drops_whole_tools_and_records_exclusions(make_app):
    app = make_app.build(default="ok")
    selection = app.tools.schemas_for(None, budget_tokens=1)
    assert selection["schemas"] == []
    assert selection["names"] == []
    assert len(selection["exclusions"]) == len(app.tools.specs())
    assert selection["tokens"] == 0
    for exclusion in selection["exclusions"]:
        assert exclusion["reason"] == "tool_schema_budget"
        assert exclusion["tokens"] >= 1
        assert exclusion["module"] == "builtin"


def test_run_persists_a_global_toolset_snapshot(make_app):
    app = make_app.build(default="ok")
    result = app.runtime.submit_user_message("hello")
    assert result.status == "completed"
    run = app.db.query_one(
        "SELECT meta FROM runs WHERE id=?", (result.run_id,)
    )
    snapshot = util.loads(run["meta"], {})["toolset"]
    assert set(snapshot["names"]) == set(app.tools.names())
    assert snapshot["modules"] == ["builtin"]
    assert snapshot["exclusions"] == []
    assert snapshot["budget_tokens"] == 2400


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


def test_tool_specs_drive_the_api_without_private_registry_access(make_app, client):
    app = make_app.build(default="ok")
    specs = app.tools.specs()
    assert {spec.name for spec in specs} == set(app.tools.names())
    payload = client(app).get("/api/tools").json()
    assert {tool["name"] for tool in payload["tools"]} == set(app.tools.names())
    assert all(tool["module"] == "builtin" for tool in payload["tools"])


def test_ui_assets_require_authentication(make_app):
    app = make_app.build(default="ok")
    unauthenticated = TestClient(Server(app).routes())
    assert unauthenticated.get("/").status_code == 401
    assert unauthenticated.get("/assets/app.js").status_code == 401
    assert unauthenticated.get("/assets/style.css").status_code == 401

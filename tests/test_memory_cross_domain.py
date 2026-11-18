"""Phase 10D: soft cross-domain memory, core profile, and unscoped General."""

from __future__ import annotations

import datetime as dt
import json

from harness import util
from harness.modules.types import Module, ModuleBinding
from harness.tools import ToolContext, ToolSpec


def _domain(app, domain_id: str, name: str, description: str = ""):
    return app.domains.create(
        domain_id,
        name=name,
        description=description,
        created_by="test",
    )


def _context(app, objective: str, domain_id: str = "general", run_id=None):
    task = app.tasks.create(
        objective=objective, domain_id=domain_id, origin="user"
    )
    domain = app.domains.get(domain_id)
    return app.context.build(
        task=task,
        domain=domain,
        trigger={"kind": "user"},
        run_id=run_id,
    )


def _register_module_tool(app, module_id, tool_name, *, scope, domain_id=None):
    app.tools.register(ToolSpec(
        name=tool_name,
        description=f"{tool_name} test tool",
        parameters={"type": "object", "properties": {}},
        risk="low",
        handler=lambda ctx, args: {"ok": True},
        module=module_id,
        scope=scope,
    ))
    app.modules.save(Module(
        id=module_id,
        kind="mcp",
        name=module_id,
        status="active",
        scope=scope,
        manifest={},
        origin="user",
        provenance={"source": "test"},
        created_by="test",
    ))
    if scope == "domain":
        assert domain_id
        app.modules.set_binding(ModuleBinding(
            module_id=module_id, domain_id=domain_id, enabled=True
        ))


def test_boost_recalls_cross_domain_fact_with_origin_label(make_app):
    app = make_app.build(default="ok")
    app.config.set("memory.cross_domain", "boost")
    health = _domain(app, "dom_health", "Health", "health and exercise")
    clothing = _domain(app, "dom_clothing", "Clothing", "wardrobe choices")
    fact = app.memory.remember(
        "The user trains for marathons at dawn.",
        kind="fact",
        scope=f"domain:{health['id']}",
        domains=app.domains,
        origin="user",
    )["memory"]

    general = _context(app, "Tell me about the user's marathon training")
    assert "trains for marathons" in general["system"]
    assert "from: Health" in general["system"]
    assert any(
        source["section"] == "memories"
        and source["ref"] == f"memory:cross:{health['id']}"
        and source["reason"] == "cross-domain memory, lexical match"
        for source in general["sources"]
    )

    sibling = _context(
        app, "Tell me about the user's marathon training", clothing["id"]
    )
    assert "trains for marathons" in sibling["system"]
    assert "from: Health" in sibling["system"]
    assert any(
        source["ref"] == f"memory:cross:{health['id']}"
        for source in sibling["sources"]
    )
    assert app.memory.get(fact["id"])["access_count"] == 0


def test_boost_never_uses_sibling_fact_as_no_hit_filler(make_app):
    app = make_app.build(default="ok")
    app.config.set("memory.cross_domain", "boost")
    health = _domain(app, "dom_health", "Health", "health and exercise")
    app.memory.remember(
        "The user trains for marathons at dawn.",
        kind="fact",
        scope=f"domain:{health['id']}",
        domains=app.domains,
        origin="user",
    )

    built = _context(app, "Arrange an entirely unrelated houseplant visit")
    assert "# Core profile" in built["system"]
    assert "trains for marathons" not in built["system"]
    assert "# Relevant memory" not in built["system"]


def test_scope_tiers_and_cross_domain_cap_keep_highest_siblings(make_app):
    app = make_app.build(default="ok")
    app.config.set("memory.cross_domain", "boost")
    current = _domain(app, "dom_current", "Current", "current work")
    sibling_a = _domain(app, "dom_sibling_a", "Sibling A", "sibling work")
    sibling_b = _domain(app, "dom_sibling_b", "Sibling B", "sibling work")
    sibling_c = _domain(app, "dom_sibling_c", "Sibling C", "sibling work")
    app.config.set("memory.cross_domain_max", 2)

    current_memory = app.memory.remember(
        "Sharedterm belongs to the current domain.",
        kind="fact", scope=f"domain:{current['id']}",
        domains=app.domains, origin="user",
    )["memory"]
    global_memory = app.memory.remember(
        "Sharedterm belongs to the global profile.",
        kind="fact", scope="global", origin="user",
    )["memory"]
    old = util.iso(util.now_utc() - dt.timedelta(days=30))
    app.db.execute(
        "UPDATE memories SET updated_at=? WHERE id IN (?,?)",
        (old, current_memory["id"], global_memory["id"]),
    )
    siblings = [
        app.memory.remember(
            f"Sharedterm belongs to sibling {index}.",
            kind="fact", scope=f"domain:{domain['id']}",
            domains=app.domains, origin="user",
        )["memory"]
        for index, domain in enumerate((sibling_a, sibling_b, sibling_c))
    ]
    app.db.execute(
        "UPDATE memories SET updated_at=? WHERE id=?",
        (old, siblings[0]["id"]),
    )

    results = app.memory.search(
        "sharedterm", scope_priority=[f"domain:{current['id']}", "global"],
        limit=10, cross_domain=True,
    )
    assert results[0]["id"] == current_memory["id"]
    assert results[1]["id"] == global_memory["id"]
    cross_ids = [item["id"] for item in results if item.get("cross_domain")]
    assert len(cross_ids) == 2
    assert set(cross_ids) == {siblings[2]["id"], siblings[1]["id"]}


def test_archived_and_retracted_sibling_rows_never_cross_over(make_app):
    app = make_app.build(default="ok")
    app.config.set("memory.cross_domain", "boost")
    health = _domain(app, "dom_health", "Health", "health and exercise")
    archived = app.memory.remember(
        "The archived sibling uses a cobalt kayak.",
        kind="fact",
        scope=f"domain:{health['id']}",
        domains=app.domains,
        origin="user",
    )["memory"]
    retracted = app.memory.remember(
        "The retracted sibling uses a vermilion canoe.",
        kind="fact",
        scope=f"domain:{health['id']}",
        domains=app.domains,
        origin="user",
    )["memory"]
    app.memory.archive(archived["id"], changed_by="test")
    app.memory.retract(retracted["id"], changed_by="test")

    archived_hits = app.memory.search(
        "cobalt kayak", scope_priority=["global"], cross_domain=True
    )
    retracted_hits = app.memory.search(
        "vermilion canoe", scope_priority=["global"], cross_domain=True
    )
    assert archived["id"] not in {item["id"] for item in archived_hits}
    assert retracted["id"] not in {item["id"] for item in retracted_hits}


def test_core_profile_is_always_on_bounded_and_deduplicated(make_app):
    app = make_app.build(default="ok")
    clothing = _domain(app, "dom_clothing", "Clothing", "wardrobe choices")
    app.config.set("memory.core_profile_chars", 90)
    pinned = app.memory.remember(
        "The user prefers a distinctive violet umbrella.",
        kind="preference",
        scope="global",
        origin="user",
    )["memory"]
    app.memory.set_pinned(pinned["id"], True, changed_by="test")
    app.memory.remember(
        "The user owns a very long collection of seasonal wool scarves "
        "in many colors that should never consume the whole prompt.",
        kind="fact",
        scope="global",
        origin="user",
    )

    # The profile reader itself has no side effects.
    profile = app.memory.core_profile()
    assert profile
    assert app.memory.get(pinned["id"])["access_count"] == 0

    general = _context(
        app, "Tell me about the distinctive violet umbrella", run_id="run_core"
    )
    core = general["system"].split("# Core profile", 1)[1].split(
        "# Relevant memory", 1
    )[0]
    assert len("# Core profile" + core) <= 90
    memory_lines = [
        line for line in general["system"].splitlines()
        if line.startswith("- [")
    ]
    assert sum(
        "distinctive violet umbrella" in line for line in memory_lines
    ) == 1
    assert any(
        source["section"] == "core_profile"
        and source["ref"] == f"memory:{pinned['id']}"
        and source["reason"] == "pinned global core profile"
        for source in general["sources"]
    )
    assert not any(
        source["section"] == "memories"
        and source["ref"] == f"memory:{pinned['id']}"
        for source in general["sources"]
    )
    assert app.memory.get(pinned["id"])["access_count"] == 1

    in_domain = _context(
        app, "Unrelated clothing planning", clothing["id"], run_id="run_domain"
    )
    assert "# Core profile" in in_domain["system"]
    assert "distinctive violet umbrella" in in_domain["system"]


def test_legacy_general_rows_read_global_and_new_general_writes_are_global(make_app):
    app = make_app.build(default="ok")
    now = util.iso()
    app.db.execute(
        "INSERT INTO memories(id, scope, domain_id, kind, content, status,"
        " origin, provenance, created_at, updated_at, revision, pinned)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "mem_legacy_general", "domain:general", "general", "fact",
            "The user keeps the legacy passport in the blue drawer.",
            "active", "user", "{}", now, now, 1, 1,
        ),
    )

    hits = app.memory.search(
        "legacy passport blue drawer", scope_priority=["global"]
    )
    hit = next(item for item in hits if item["id"] == "mem_legacy_general")
    assert hit["effective_scope"] == "global"
    assert hit["score_parts"]["scope"] == 0.3
    assert any(
        item["id"] == "mem_legacy_general"
        for item in app.memory.core_profile()
    )

    created = app.memory.remember(
        "The user prefers a paper map for unfamiliar cities.",
        kind="preference",
        scope="domain:general",
        domain_id="general",
        domains=app.domains,
        origin="user",
    )["memory"]
    assert created["scope"] == "global"
    assert created["domain_id"] is None


def test_composite_clothing_memory_and_global_weather_tool_compose(make_app):
    app = make_app.build(default="ok")
    app.config.set("memory.cross_domain", "boost")
    clothing = _domain(app, "dom_clothing", "Clothing", "wardrobe choices")
    app.memory.remember(
        "The user wears a slate raincoat in drizzle.",
        kind="preference",
        scope=f"domain:{clothing['id']}",
        domains=app.domains,
        origin="user",
    )
    _register_module_tool(app, "mod_weather", "weather_now", scope="global")
    _register_module_tool(
        app,
        "mod_clothing",
        "clothing_wardrobe",
        scope="domain",
        domain_id=clothing["id"],
    )

    general = _context(app, "What raincoat should I wear in drizzle?")
    assert "slate raincoat" in general["system"]
    assert "from: Clothing" in general["system"]
    general_tools = app.runtime._select_tools("general", "user")
    assert "weather_now" in general_tools["names"]
    assert "clothing_wardrobe" not in general_tools["names"]

    in_clothing = _context(
        app, "What raincoat should I wear in drizzle?", clothing["id"]
    )
    assert "slate raincoat" in in_clothing["system"]
    clothing_tools = app.runtime._select_tools(clothing["id"], "user")
    assert "weather_now" in clothing_tools["names"]
    assert "clothing_wardrobe" in clothing_tools["names"]


def test_search_knowledge_search_mode_crosses_and_labels_origin(make_app):
    app = make_app.build(default="ok")
    health = _domain(app, "dom_health", "Health", "health and exercise")
    clothing = _domain(app, "dom_clothing", "Clothing", "wardrobe choices")
    sibling = app.memory.remember(
        "The user drinks chrysanthemum tea after running.",
        kind="preference",
        scope=f"domain:{health['id']}",
        domains=app.domains,
        origin="user",
    )["memory"]
    ctx = ToolContext(
        run_id="run_search",
        task_id="task_search",
        domain_id=clothing["id"],
        memory=app.memory,
        domains=app.domains,
        messages=app.messages,
    )

    app.config.set("memory.cross_domain", "search")
    found = app.tools._tool_search_knowledge(
        ctx, {"query": "chrysanthemum tea after running"}
    )
    result = next(item for item in found["memories"] if item["id"] == sibling["id"])
    assert result["origin"] == "Health"
    assert result["from"] == "Health"
    assert app.memory.get(sibling["id"])["access_count"] == 1

    app.config.set("memory.cross_domain", "off")
    hidden = app.tools._tool_search_knowledge(
        ctx, {"query": "chrysanthemum tea after running"}
    )
    assert sibling["id"] not in {item["id"] for item in hidden["memories"]}
    assert app.memory.get(sibling["id"])["access_count"] == 1


def test_consolidation_still_ignores_archived_rows(make_app):
    app = make_app.build(default="ok")
    archived = app.memory.remember(
        "The archived consolidation fact must not appear.",
        kind="fact",
        scope="global",
        origin="user",
    )["memory"]
    app.memory.archive(archived["id"], changed_by="test")
    app.model.scripts.append({"content": json.dumps({"changes": []})})

    report = app.consolidation.run(trigger="manual")
    assert report["status"] in ("no_changes", "nothing_to_do")
    assert "archived consolidation fact" not in json.dumps(app.model.last_messages)

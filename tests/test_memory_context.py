"""D. Context and memory: capture, scopes, retrieval, provenance, budgets,
consolidation, and artifact consistency."""

from __future__ import annotations

import json

from harness import util


def _say(app, text, source="web"):
    return app.runtime.submit_user_message(text, source=source)


def test_explicit_remember_creates_durable_memory(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "The user prefers concise bullet-point summaries.",
            "kind": "preference"}}]},
        {"content": "Remembered."},
    ])
    _say(app, "Remember that I prefer concise bullet-point summaries.")
    memories = app.memory.list()
    assert len(memories) == 1
    assert memories[0]["kind"] == "preference"
    assert memories[0]["status"] == "active"
    assert memories[0]["provenance"]["run_id"]


def test_old_memory_retrieved_after_many_later_messages(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "The synology nas has IP 10.0.0.7.",
            "kind": "fact"}}]},
        {"content": "Saved."},
    ])
    _say(app, "Remember that the synology nas has IP 10.0.0.7.")
    # Bury it under many later messages.
    for index in range(12):
        app.model.scripts.append({"content": f" chatter {index}"})
        _say(app, f"unrelated chatter number {index}")
    # The next question must retrieve the old fact (FTS + context history).
    app.model.scripts.append({"content": "The synology is at 10.0.0.7."})
    _say(app, "what is the synology nas ip address?")
    context = json.dumps(app.model.last_messages)
    assert "10.0.0.7" in context


def test_global_and_domain_scopes_respected(make_app):
    app = make_app.build(default="ok")
    garden = app.domains.create(util.new_id("dom"), name="Garden",
                                description="plants", created_by="test")
    app.memory.remember("Global preference: always answer in English.",
                        kind="preference", scope="global", origin="user")
    app.memory.remember("Garden rule: water in the evening only.",
                        kind="constraint", scope=f"domain:{garden['id']}",
                        domains=app.domains, origin="user")
    # A garden query sees both.
    results = app.memory.search("garden watering rule",
                                scope_priority=[f"domain:{garden['id']}", "global"])
    scopes = {r["scope"] for r in results}
    assert scopes == {"global", f"domain:{garden['id']}"}
    # An unrelated domain query never returns the garden-scoped memory.
    results = app.memory.search("server maintenance",
                                scope_priority=["global"])
    contents = " ".join(r["content"] for r in results)
    assert "water in the evening" not in contents


def test_scope_boundary_holds_on_lexical_hit_and_detail_is_reachable(make_app, client):
    app = make_app.build(default="ok")
    garden = app.domains.create(util.new_id("dom"), name="Garden",
                                description="plants", created_by="test")
    garden_memory = app.memory.remember(
        "Water the tomatoes at dusk, never at midday.",
        kind="constraint", scope=f"domain:{garden['id']}",
        domains=app.domains, origin="user",
    )["memory"]
    app.memory.remember(
        "The water filter is due for replacement.",
        kind="fact", scope="global", origin="user",
    )
    # "water" is a lexical hit in both, but a global-only query must not
    # surface a sibling domain's memory.
    results = app.memory.search("water", scope_priority=["global"])
    assert all(item["scope"] == "global" for item in results)
    # Naming the domain in the priority list makes it visible.
    results = app.memory.search(
        "water", scope_priority=["global", f"domain:{garden['id']}"]
    )
    assert {item["scope"] for item in results} == {
        "global", f"domain:{garden['id']}",
    }
    app.memory.retract(garden_memory["id"], changed_by="test",
                       reason="scope test cleanup")
    assert app.memory.get(garden_memory["id"])["status"] == "retracted"
    listed = app.memory.list(status="any")
    assert any(item["id"] == garden_memory["id"] for item in listed)
    http = client(app)
    response = http.get(f"/api/memories/{garden_memory['id']}")
    assert response.status_code == 200
    assert response.json()["memory"]["status"] == "retracted"


def test_context_retrieval_identifies_its_sources(make_app):
    app = make_app.build(default="ok")
    app.memory.remember("The unifi console is at 10.0.0.9.",
                        kind="fact", scope="global", origin="user")
    task = app.tasks.create(objective="check the unifi console", origin="user")
    built = app.context.build(task=task, domain=None, trigger={"kind": "user"})
    refs = [s["ref"] for s in built["sources"]]
    assert any(r and r.startswith("memory:") for r in refs)
    assert any(s["section"] == "memories" and s["reason"] for s in built["sources"])


def test_context_budgets_prevent_unbounded_history(make_app):
    app = make_app.build(default="ok")
    app.config.set("context.total_budget_tokens", 3000)  # ~12000 chars
    task = app.tasks.create(objective="budget check", origin="user")
    for index in range(60):
        app.messages.record("user", f"message {index} " + "x" * 200,
                            source="web")
    built = app.context.build(task=task, domain=None, trigger={"kind": "user"})
    dropped = [s for s in built["sources"]
               if s["reason"] and "budget" in s["reason"]]
    assert len(built["system"]) < 3000 * 5
    assert dropped or len(built["system"]) < 3000 * 4


def test_correction_supersedes_and_records_revision(make_app):
    app = make_app.build(default="ok")
    first = app.memory.remember("The rack is 40U high.", kind="fact",
                                scope="global", origin="user")
    second = app.memory.remember("The rack is 42U high.", kind="fact",
                                  scope="global", origin="user")
    assert second["superseded"] is not None
    assert first["memory"]["id"] == second["superseded"]["id"]
    active = [m for m in app.memory.list()]
    assert len(active) == 1 and "42U" in active[0]["content"]
    old = app.memory.revisions(first["memory"]["id"])
    assert old[0]["before"]["content"] == "The rack is 40U high."
    # Provenance preserved: the superseded row points at its replacement.
    row = app.db.query_one(
        "SELECT superseded_by, status FROM memories WHERE id=?",
        (first["memory"]["id"],),
    )
    assert row["superseded_by"] == second["memory"]["id"]
    assert row["status"] == "superseded"


def test_update_memory_creates_revision(make_app):
    app = make_app.build(default="ok")
    created = app.memory.remember("Meeting is Tuesday.", kind="fact",
                                  scope="global", origin="user")
    updated = app.memory.update(created["memory"]["id"],
                               content="Meeting is Wednesday.",
                               changed_by="agent", reason="user correction")
    assert updated["revision"] == 2
    revisions = app.memory.revisions(created["memory"]["id"])
    assert revisions[0]["reason"] == "user correction"
    assert revisions[0]["before"]["content"] == "Meeting is Tuesday."


def test_memory_update_via_tool(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "The printer is in room 12.", "kind": "fact"}}]},
        {"content": "Saved."},
    ])
    _say(app, "remember the printer location")
    memory_id = app.memory.list()[0]["id"]
    app.model.scripts.append({"tool_calls": [{"name": "update_memory", "arguments": {
        "memory_id": memory_id, "content": "The printer is in room 14.",
        "reason": "it moved"}}]})
    app.model.scripts.append({"content": "Updated."})
    _say(app, "correct the printer location")
    memories = app.memory.list()
    assert "room 14" in memories[0]["content"]
    assert app.memory.revisions(memory_id)


def test_temp_memory_expires(make_app):
    import datetime as dt

    app = make_app.build(default="ok")
    app.memory.remember("The build is broken on main right now.",
                        kind="temp", scope="global", origin="user")
    row = app.memory.list()[0]
    assert row["expires_at"] is not None
    # Force expiry and run the sweep.
    app.db.execute("UPDATE memories SET expires_at=? WHERE id=?",
                   (util.iso(util.now_utc() - dt.timedelta(minutes=1)),
                    row["id"]))
    expired = app.memory.expire_stale()
    assert expired == 1
    assert app.memory.list() == []
    assert app.db.query_one(
        "SELECT status FROM memories WHERE id=?", (row["id"],)
    )["status"] == "retracted"


def test_consolidation_merges_and_records_provenance(make_app):
    app = make_app.build(default="ok")
    # Two wording-different duplicates (below the conflict threshold so both
    # stay active until consolidation merges them).
    a = app.memory.remember("The wiki is hosted on the proxmox node.",
                            kind="fact", scope="global", origin="user")
    b = app.memory.remember("Wiki hosting: the proxmox machine.",
                            kind="fact", scope="global", origin="user")
    app.messages.record("user", "The printer is in room 14 now.", source="web")
    app.model.scripts.append({"content": json.dumps({"changes": [
        {"op": "merge", "source_id": b["memory"]["id"],
         "target_id": a["memory"]["id"]},
        {"op": "create", "content": "The printer is in room 14.",
         "kind": "fact", "scope": "global"},
        {"op": "retract", "memory_id": "mem_nonexistent",
         "reason": "junk"},
    ]})})
    report = app.consolidation.run(trigger="manual")
    assert report["status"] == "completed"
    assert len(report["applied"]) == 2
    assert len(report["rejected"]) == 1
    assert report["log_path"]
    active = {m["id"]: m for m in app.memory.list()}
    assert b["memory"]["id"] not in active
    assert any("printer" in m["content"] for m in active.values())
    # The change log is readable.
    log_text = (app.artifacts_dir / report["log_path"]).read_text(encoding="utf-8")
    assert "merge" in log_text


def test_consolidation_failure_changes_nothing(make_app):
    app = make_app.build(default="ok")
    app.memory.remember("stable fact", kind="fact", scope="global", origin="user")
    app.model.scripts.append({"error": "endpoint down"})
    report = app.consolidation.run(trigger="manual")
    assert report["status"] == "failed"
    assert app.memory.list() and "stable fact" in app.memory.list()[0]["content"]


def test_artifact_database_inconsistency_detected(make_app):
    app = make_app.build(default="ok")
    created = app.memory.remember(
        "Long note: " + "detail " * 200, kind="context", scope="global",
        origin="user", artifact_body="# Long note\n" + "line\n" * 300,
    )
    problems = app.memory.check_artifacts()
    assert problems == []
    # Simulate a deleted artifact (DB is authoritative; must be detected).
    (app.artifacts_dir / created["memory"]["artifact_path"]).unlink()
    problems = app.memory.check_artifacts()
    assert problems and problems[0]["problem"] == "missing"


def test_artifact_read_bounded_and_traversal_blocked(make_app):
    import pytest

    app = make_app.build(default="ok")
    created = app.memory.remember("long note", kind="context", scope="global",
                                  origin="user",
                                  artifact_body="# note\n" + "x" * 500)
    rel = created["memory"]["artifact_path"]
    assert rel
    body = app.memory.read_artifact(rel)
    assert body.startswith("# note")
    with pytest.raises(ValueError):
        app.memory.read_artifact("../../harness.db")
    with pytest.raises(ValueError):
        app.memory.read_artifact("missing/file.md")

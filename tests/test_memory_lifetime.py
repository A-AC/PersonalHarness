"""Phase 3: usage-based memory lifetime, archive, restore, and retrieval.

Retention is deterministic (no model call), archived rows are a separate
state from expired/retracted rows, and explicit retrieval is the only path
that restores an archived memory.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from harness import util
from harness.db import Database, SCHEMA_VERSION


def _say(app, text, source="web"):
    return app.runtime.submit_user_message(text, source=source)


def _backdate(app, memory_id: str, days: int, column: str = "created_at"):
    old = util.iso(util.now_utc() - dt.timedelta(days=days))
    app.db.execute(
        f"UPDATE memories SET {column}=? WHERE id=?", (old, memory_id)
    )


def test_schema_migration_from_v3_adds_lifetime_columns(tmp_path):
    """A real v3 database gains the v4 columns without a table rebuild."""
    path = tmp_path / "harness.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta(key, value) VALUES ('schema_version', '3');
            CREATE TABLE memories (
              row_num INTEGER PRIMARY KEY AUTOINCREMENT,
              id TEXT NOT NULL UNIQUE,
              scope TEXT NOT NULL,
              domain_id TEXT,
              kind TEXT NOT NULL,
              content TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'active',
              origin TEXT NOT NULL,
              provenance TEXT NOT NULL,
              artifact_path TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              expires_at TEXT,
              revision INTEGER NOT NULL DEFAULT 1,
              supersedes_id TEXT,
              superseded_by TEXT,
              tags TEXT,
              meta TEXT
            );
            INSERT INTO memories(id, scope, kind, content, status, origin,
              provenance, created_at, updated_at)
            VALUES ('mem_old', 'global', 'fact', 'kept across migration',
              'active', 'user', '{}', '2026-01-01T00:00:00Z',
              '2026-01-01T00:00:00Z');
            """
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    try:
        version = db.query_one(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        )["value"]
        assert int(version) == SCHEMA_VERSION == 10
        columns = {
            row["name"] for row in db.query("PRAGMA table_info(memories)")
        }
        assert {
            "access_count", "last_accessed_at", "archived_at", "pinned",
            "importance",
        } <= columns
        row = db.query_one(
            "SELECT content, access_count, archived_at, pinned FROM memories"
            " WHERE id='mem_old'"
        )
        assert row["content"] == "kept across migration"
        assert row["access_count"] == 0
        assert row["archived_at"] is None
        assert row["pinned"] == 0
        tables = {
            item["name"] for item in db.query(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "memory_access" in tables
    finally:
        db.close()


def test_record_access_bumps_once_per_run_and_batches(make_app):
    app = make_app.build(default="ok")
    first = app.memory.remember("Fact one is stable.", kind="fact",
                                scope="global", origin="user")["memory"]
    second = app.memory.remember("Fact two is stable.", kind="fact",
                                 scope="global", origin="user")["memory"]

    out = app.memory.record_access([first["id"], second["id"]], run_id="run_a")
    assert out["updated"] == 2
    assert app.memory.get(first["id"])["access_count"] == 1
    assert app.memory.get(second["id"])["access_count"] == 1
    assert app.memory.get(first["id"])["last_accessed_at"]

    # Same run again: deduped, no double count.
    again = app.memory.record_access([first["id"], second["id"]],
                                     run_id="run_a")
    assert again["updated"] == 0
    assert app.memory.get(first["id"])["access_count"] == 1

    # A new run counts once more.
    app.memory.record_access([first["id"]], run_id="run_b")
    assert app.memory.get(first["id"])["access_count"] == 2
    assert app.memory.get(second["id"])["access_count"] == 1

    # Unknown ids are ignored, never crash the batch.
    missing = app.memory.record_access(["mem_missing", first["id"]],
                                       run_id="run_c")
    assert missing["updated"] == 1


def test_unused_memory_archives_while_used_memory_survives(make_app):
    app = make_app.build(default="ok")
    app.config.set("memory.lifetime.base_days", 30)
    app.config.set("memory.lifetime.min_days", 14)
    app.config.set("memory.lifetime.max_days", 1095)
    unused = app.memory.remember("The unused spare cable is in the attic.",
                                 kind="fact", scope="global",
                                 origin="user")["memory"]
    used = app.memory.remember("The router admin page is at 10.0.0.1.",
                               kind="fact", scope="global",
                               origin="user")["memory"]
    for index in range(20):
        app.memory.record_access([used["id"]], run_id=f"run_{index}")
    _backdate(app, unused["id"], 100)
    _backdate(app, used["id"], 100, column="last_accessed_at")

    report = app.memory.sweep_lifetime(util.now_utc())
    assert report["status"] == "completed"
    assert unused["id"] in report["archived"]
    assert app.memory.get(unused["id"])["archived_at"]
    # 20 accesses extend retention past the 100-day age.
    assert app.memory.get(used["id"])["archived_at"] is None
    assert app.memory.get(used["id"])["access_count"] == 20


def test_pinned_and_protected_kinds_never_archive(make_app):
    app = make_app.build(default="ok")
    app.config.set("memory.lifetime.base_days", 1)
    app.config.set("memory.lifetime.min_days", 1)
    app.config.set("memory.lifetime.max_days", 1)
    pinned = app.memory.remember("The pinned fact survives.",
                                 kind="fact", scope="global",
                                 origin="user")["memory"]
    protected = app.memory.remember("We decided to keep the rack.",
                                    kind="decision", scope="global",
                                    origin="user")["memory"]
    plain = app.memory.remember("A plain old fact.",
                                kind="fact", scope="global",
                                origin="user")["memory"]
    app.memory.set_pinned(pinned["id"], True, changed_by="test")
    for memory in (pinned, protected, plain):
        _backdate(app, memory["id"], 30)

    report = app.memory.sweep_lifetime(util.now_utc())
    assert plain["id"] in report["archived"]
    assert app.memory.get(pinned["id"])["archived_at"] is None
    assert app.memory.get(protected["id"])["archived_at"] is None
    assert app.memory.get(plain["id"])["archived_at"] is not None


def test_archived_memory_read_does_not_restore_without_explicit_choice(make_app):
    app = make_app.build(default="ok")
    memory = app.memory.remember("The old wifi password was hunter2.",
                                 kind="fact", scope="global",
                                 origin="user")["memory"]
    app.memory.archive(memory["id"], changed_by="test", reason="test archive")

    default = app.memory.search("wifi password", scope_priority=["global"])
    assert memory["id"] not in {item["id"] for item in default}
    task = app.tasks.create(objective="what was the old wifi password",
                            origin="user")
    built = app.context.build(
        task=task, domain=None, trigger={"kind": "user"}, run_id="run_ctx"
    )
    assert "hunter2" not in built["system"]

    included = app.memory.search(
        "wifi password", scope_priority=["global"], include_archived=True,
    )
    hit = next(item for item in included if item["id"] == memory["id"])
    assert hit["archived"] is True
    assert not hit.get("restored")
    assert hit["score_parts"]["archived"] < 0
    assert "archived" in hit["reason"]
    assert app.memory.get(memory["id"])["archived_at"] is not None

    restored = app.memory.search(
        "wifi password", scope_priority=["global"], include_archived=True,
        restore_archived=True,
    )
    hit = next(item for item in restored if item["id"] == memory["id"])
    assert hit["restored"] is True
    assert app.memory.get(memory["id"])["archived_at"] is None
    assert any("explicit retrieval" in (rev.get("reason") or "")
               for rev in app.memory.revisions(memory["id"]))


def test_search_knowledge_tool_can_reach_archived_memory(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "search_knowledge", "arguments": {
            "query": "vault code", "include_archived": True,
            "restore_archived": True}}]},
        {"content": "I found the archived note."},
    ])
    memory = app.memory.remember("The vault code is 4411.",
                                 kind="fact", scope="global",
                                 origin="user")["memory"]
    app.memory.archive(memory["id"], changed_by="test")

    result = _say(app, "look up the vault code, including archived notes")
    assert result.status == "completed"
    stored = app.memory.get(memory["id"])
    assert stored["archived_at"] is None
    # The tool result is recorded and the memory now shows one use.
    assert stored["access_count"] == 1
    executions = app.tools.executions_for_run(result.run_id)
    assert any(execution["tool"] == "search_knowledge"
               and execution["status"] == "succeeded"
               for execution in executions)


def test_consolidation_ignores_archived_rows(make_app):
    app = make_app.build(default="ok")
    archived = app.memory.remember(
        "The archived zebra fact must not resurface.",
        kind="fact", scope="global", origin="user",
    )["memory"]
    app.memory.archive(archived["id"], changed_by="test")
    app.model.scripts.append({"content": json.dumps({"changes": []})})

    report = app.consolidation.run(trigger="manual")
    assert report["status"] in ("no_changes", "nothing_to_do")
    prompt = json.dumps(app.model.last_messages)
    assert "archived zebra fact" not in prompt
    assert app.memory.get(archived["id"])["archived_at"] is not None


def test_heartbeat_sweep_is_bounded_and_recorded(make_app):
    app = make_app.build(default="ok")
    app.config.set("memory.lifetime.base_days", 1)
    app.config.set("memory.lifetime.min_days", 1)
    app.config.set("memory.lifetime.max_days", 1)
    app.config.set("memory.lifetime.sweep_interval_s", 0)
    app.config.set("memory.lifetime.sweep_batch", 2)
    contents = [
        "Apples grow in orchards.",
        "Bananas ripen quickly.",
        "Cobalt is a metal.",
        "Dolphins are mammals.",
        "Everest is very tall.",
    ]
    ids = [
        app.memory.remember(content, kind="fact", scope="global",
                            origin="user")["memory"]["id"]
        for content in contents
    ]
    for memory_id in ids:
        _backdate(app, memory_id, 10)

    report = app.scheduler.heartbeat()
    assert report["memory"]["status"] == "completed"
    assert report["memory"]["examined"] == 2
    assert len(report["memory"]["archived"]) == 2
    archived = app.memory.list(status="archived", limit=10)
    assert len(archived) == 2
    assert app.db.meta_get("memory.lifetime.last_sweep")


def test_memory_api_archive_restore_filters_and_usage(make_app, client):
    app = make_app.build(default="ok")
    memory = app.memory.remember("The garage door code is 7788.",
                                 kind="fact", scope="global",
                                 origin="user")["memory"]
    http = client(app)

    archived = http.post(f"/api/memories/{memory['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["memory"]["archived"] is True

    active = http.get("/api/memories?status=active").json()["memories"]
    assert memory["id"] not in {item["id"] for item in active}
    archived_list = http.get(
        "/api/memories?status=archived"
    ).json()["memories"]
    assert memory["id"] in {item["id"] for item in archived_list}

    search = http.post("/api/memories/search", json={
        "query": "garage door code", "include_archived": True,
    }).json()
    assert search["results"]
    assert search["results"][0]["archived"] is True
    assert search["results"][0]["access_count"] == 1

    restored = http.post(f"/api/memories/{memory['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["memory"]["archived"] is False
    active = http.get("/api/memories?status=active").json()["memories"]
    assert memory["id"] in {item["id"] for item in active}

    pinned = http.post(f"/api/memories/{memory['id']}/pin")
    assert pinned.status_code == 200
    assert pinned.json()["memory"]["pinned"] is True
    unpinned = http.post(f"/api/memories/{memory['id']}/unpin")
    assert unpinned.json()["memory"]["pinned"] is False


def test_context_access_counts_only_final_model_visible_memory(make_app):
    app = make_app.build(default="ok")
    memory = app.memory.remember("The backup drive is labelled vault.",
                                 kind="fact", scope="global",
                                 origin="user")["memory"]

    first = app.runtime.submit_user_message("where is the backup drive")
    assert first.status == "completed"
    assert app.memory.get(memory["id"])["access_count"] == 1

    # A tiny section budget clips away the memory marker before the model call.
    # The candidate remains in the audit ledger as excluded and is not charged
    # as a second disclosure.
    budgets = dict(app.config.get("context.section_budgets", {}))
    budgets["memories"] = 1
    app.config.set("context.section_budgets", budgets)
    second = app.runtime.submit_user_message("where is the backup drive")
    assert second.status == "completed"
    assert app.memory.get(memory["id"])["access_count"] == 1
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (second.run_id,))
    sources = util.loads(run["context_sources"], [])
    memory_sources = [s for s in sources if s.get("memory_id") == memory["id"]]
    assert memory_sources
    assert all(s["disposition"] == "excluded" for s in memory_sources)




def test_transcript_compaction_updates_ledger_without_double_counting(make_app):
    app = make_app.build(default="ok")
    memory = app.memory.remember("The compaction target is unique.",
                                 kind="fact", scope="global",
                                 origin="user")["memory"]
    result = app.runtime.submit_user_message("compaction target")
    assert result.status == "completed"
    assert app.memory.get(memory["id"])["access_count"] == 1

    # Simulate the later per-call compaction removing the memory section. The
    # durable transcript is unchanged, but the final-payload ledger is not.
    app.runtime._reconcile_prompt_context(
        result.run_id,
        [{"role": "system", "content": "# Current situation"}],
        ["clipped system prompt"],
    )
    assert app.memory.get(memory["id"])["access_count"] == 1
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    sources = util.loads(run["context_sources"], [])
    target = [s for s in sources if s.get("memory_id") == memory["id"]]
    assert target and all(s["disposition"] == "excluded" for s in target)
    assert any(
        s.get("disposition") == "transformed"
        and "clipped system prompt" in s.get("reason", "")
        for s in sources
    )



def test_context_retry_reconciles_tighter_payload(make_app, monkeypatch):
    from harness import transcript as transcript_mod
    from harness.model import ModelContextError, ScriptedModel

    class ContextOnceModel(ScriptedModel):
        def chat(self, messages, tools, **kwargs):
            if not self.calls:
                self.calls.append({"messages": messages, "tools": tools})
                raise ModelContextError("prompt too long")
            return super().chat(messages, tools, **kwargs)

    model = ContextOnceModel(default="done")
    app = make_app.build(model=model)
    memory = app.memory.remember("The retry memory is visible only initially.",
                                 kind="fact", scope="global",
                                 origin="user")["memory"]
    original = transcript_mod.compact
    calls = 0

    def shrinking(messages, budget, tools_schema=None):
        nonlocal calls
        calls += 1
        prompt, repairs = original(messages, budget, tools_schema)
        if calls == 2:
            prompt[0]["content"] = "# Current situation"
            repairs.append("tighter test compaction")
        return prompt, repairs

    monkeypatch.setattr(transcript_mod, "compact", shrinking)
    result = app.runtime.submit_user_message("retry memory")
    assert result.status == "completed"
    assert calls == 2
    # The initial attempted payload did disclose the memory once; the tighter
    # retry excludes it and is reflected in the ledger without a second count.
    assert app.memory.get(memory["id"])["access_count"] == 1
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    sources = util.loads(run["context_sources"], [])
    target = [s for s in sources if s.get("memory_id") == memory["id"]]
    assert target and all(s["disposition"] == "excluded" for s in target)
    assert any("tighter test compaction" in s.get("reason", "") for s in sources)

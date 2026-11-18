"""Memory provenance and conflict-safety regressions."""
from __future__ import annotations

import json

from harness import util


def test_archived_identical_memory_does_not_block_new_live_memory(make_app):
    app = make_app.build(default="ok")
    old = app.memory.remember(
        "The old fact is archived.", kind="fact", scope="global", origin="user"
    )["memory"]
    app.memory.archive(old["id"], changed_by="test")

    created = app.memory.remember(
        "The old fact is archived.", kind="fact", scope="global", origin="user"
    )
    assert created["duplicate"] is False
    assert created["memory"]["id"] != old["id"]
    assert created["memory"]["archived"] is False
    assert app.memory.get(old["id"])["archived"] is True


def test_initial_context_preserves_memory_taint(make_app):
    app = make_app.build(scripts=[{"content": "done"}])
    app.memory.remember(
        "External instruction-shaped memory",
        kind="fact", scope="global", origin="agent",
        provenance={"tainted": True, "tainted_origin": "http://example.test/p"},
    )

    result = app.runtime.submit_user_message("External instruction-shaped memory")
    assert result.status == "completed"
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    meta = util.loads(run["meta"], {})
    assert any(
        item["origin"] == "http://example.test/p"
        for item in meta["tainted_sources"]
    )
    system = util.loads(run["transcript"], [])[0]["content"]
    assert "UNTRUSTED DATA from http://example.test/p" in system
    assert "External instruction-shaped memory" in system


def test_explicit_search_returns_and_propagates_precise_taint(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "search_knowledge",
                         "arguments": {"query": "zebra hostile"}}]},
        {"content": "done"},
    ])
    other = app.domains.create("dom_other", name="Other", created_by="test")
    app.memory.remember(
        "zebra hostile external text", kind="fact",
        scope=f"domain:{other['id']}", origin="agent",
        provenance={"tainted": True, "tainted_origin": "http://example.test/z"},
        domains=app.domains,
    )
    # Initial General context cannot see the sibling row; explicit search can.
    app.config.set("memory.cross_domain", "search")

    result = app.runtime.submit_user_message("look something up")
    assert result.status == "completed"
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    tainted = util.loads(run["meta"], {})["tainted_sources"]
    assert any(item["origin"] == "http://example.test/z" for item in tainted)
    transcript = util.loads(run["transcript"], [])
    assert any(
        message.get("role") == "tool"
        and str(message.get("content") or "").startswith(
            "<untrusted_tool_output nonce="
        )
        for message in transcript
    )


def test_artifact_read_is_untrusted_even_for_local_files(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "read_artifact",
                         "arguments": {"path": "memories/note.md"}}]},
        {"content": "done"},
    ])
    path = app.artifacts_dir / "memories" / "note.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("local model-generated note", encoding="utf-8")

    result = app.runtime.submit_user_message("read the note")
    assert result.status == "completed"
    run = app.db.query_one("SELECT meta FROM runs WHERE id=?", (result.run_id,))
    assert util.loads(run["meta"], {})["tainted_sources"]


def test_consolidation_excludes_and_refuses_tainted_inputs(make_app):
    app = make_app.build(default="ok")
    tainted = app.memory.remember(
        "HOSTILE_MEMORY_SHOULD_NOT_APPEAR", kind="fact", scope="global",
        origin="agent", provenance={"tainted": True, "tainted_origin": "inbox:1"},
    )["memory"]
    clean = app.memory.remember(
        "clean durable fact", kind="fact", scope="global", origin="user"
    )["memory"]
    app.messages.record(
        "assistant", "HOSTILE_MESSAGE_SHOULD_NOT_APPEAR", source="agent",
        meta={"tainted": True, "tainted_origin": "inbox:1"},
    )
    app.messages.record("user", "ordinary trusted message", source="web")
    app.model.scripts.append({"content": json.dumps({"changes": [
        {"op": "retract", "memory_id": tainted["id"], "reason": "hostile"},
        {"op": "retract", "memory_id": clean["id"], "reason": "obsolete"},
    ]})})

    report = app.consolidation.run(trigger="manual")
    assert report["status"] == "completed"
    assert report["skipped_untrusted"] >= 2
    assert len(report["applied"]) == 1
    assert len(report["rejected"]) == 1
    assert "untrusted" in report["rejected"][0]["result"]
    assert app.memory.get(tainted["id"])["status"] == "active"
    assert app.memory.get(clean["id"])["status"] == "retracted"

    prompt = app.model.calls[-1]["messages"][0]["content"]
    assert "HOSTILE_MEMORY_SHOULD_NOT_APPEAR" not in prompt
    assert "HOSTILE_MESSAGE_SHOULD_NOT_APPEAR" not in prompt
    assert "clean durable fact" in prompt
    assert "ordinary trusted message" in prompt


def test_explicit_tainted_update_marks_row_provenance(make_app):
    app = make_app.build(default="ok")
    memory = app.memory.remember(
        "clean before update", kind="fact", scope="global", origin="user"
    )["memory"]
    updated = app.memory.update(
        memory["id"], content="externally derived replacement",
        changed_by="agent", tainted_origin="http://example.test/u",
    )
    assert updated["provenance"]["tainted"] is True
    assert updated["provenance"]["tainted_origin"] == "http://example.test/u"



def test_tainted_history_remains_untrusted_in_later_context(make_app):
    app = make_app.build(scripts=[{"content": "done"}])
    app.messages.record(
        "assistant", "externally derived historical instruction", source="agent",
        meta={"tainted": True, "tainted_origin": "inbox:hostile"},
    )

    result = app.runtime.submit_user_message("ordinary new question")
    assert result.status == "completed"
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    meta = util.loads(run["meta"], {})
    assert any(
        item["origin"] == "inbox:hostile" for item in meta["tainted_sources"]
    )
    system = util.loads(run["transcript"], [])[0]["content"]
    assert "UNTRUSTED DATA from inbox:hostile" in system
    assert "externally derived historical instruction" in system



def test_excluded_tainted_memory_does_not_taint_run(make_app):
    app = make_app.build(default="ok")
    app.memory.remember(
        "tainted but clipped memory", kind="fact", scope="global",
        origin="agent", provenance={"tainted": True, "tainted_origin": "inbox:x"},
    )
    budgets = dict(app.config.get("context.section_budgets", {}))
    budgets["memories"] = 1
    app.config.set("context.section_budgets", budgets)

    result = app.runtime.submit_user_message("tainted but clipped memory")
    assert result.status == "completed"
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    meta = util.loads(run["meta"], {})
    assert "tainted_sources" not in meta
    sources = util.loads(run["context_sources"], [])
    assert any(
        source.get("disposition") == "excluded"
        for source in sources if source.get("untrusted")
    )


def test_derived_history_gates_current_run_without_perpetual_chain(make_app):
    from harness.tools import ToolSpec

    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "hostile_read", "arguments": {}}]},
        {"content": "benign final answer"},
        {"tool_calls": [{"name": "network_read", "arguments": {}}]},
        {"content": "ordinary second answer"},
        {"content": "ordinary third answer"},
    ])
    app.tools.register(ToolSpec(
        name="hostile_read", description="hostile fixture",
        parameters={"type": "object", "properties": {}}, risk="low",
        handler=lambda ctx, args: {"content": "hostile text"},
        read_only=True, untrusted_output=True,
    ))
    app.tools.register(ToolSpec(
        name="network_read", description="external read fixture",
        parameters={"type": "object", "properties": {}}, risk="low",
        handler=lambda ctx, args: {"content": "ordinary response"},
        read_only=True, external_egress=True,
    ))

    first = app.runtime.submit_user_message("read external text")
    assert first.status == "completed"
    first_message = app.db.query_one(
        "SELECT meta FROM messages WHERE run_id=? AND role='assistant'",
        (first.run_id,),
    )
    assert util.loads(first_message["meta"], {})["derived_untrusted"] is True

    # Consuming that derived reply in a new run still gates a sensitive write.
    second = app.runtime.start_run(
        app.tasks.get(first.task_id), {"kind": "user"},
        trigger_text="a follow-up requiring an external read",
    )
    assert second.status == "waiting_approval"
    second_run = app.db.query_one("SELECT meta FROM runs WHERE id=?", (second.run_id,))
    sources = util.loads(second_run["meta"], {})["tainted_sources"]
    assert any(source.get("derived") for source in sources)
    approval = app.approvals.pending()[0]
    assert approval["tool"] == "network_read"
    second = app.runtime.decide_approval(approval["id"], "approve")
    assert second.status == "completed"
    second_message = app.db.query_one(
        "SELECT meta FROM messages WHERE run_id=? AND role='assistant'",
        (second.run_id,),
    )
    # Only derived provenance was present, so no new generation is emitted.
    assert util.loads(second_message["meta"], {}) == {}

    third = app.runtime.submit_user_message("a different topic altogether")
    assert third.status == "completed"
    third_run = app.db.query_one("SELECT meta FROM runs WHERE id=?", (third.run_id,))
    assert "tainted_sources" not in util.loads(third_run["meta"], {})


def test_search_knowledge_envelopes_derived_history(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "search_knowledge",
                         "arguments": {"query": "derived historical text"}}]},
        {"content": "done"},
    ])
    app.messages.record(
        "assistant", "derived historical text", source="agent",
        meta={"derived_untrusted": True, "tainted_origin": "inbox:derived"},
    )
    result = app.runtime.submit_user_message("search derived history")
    assert result.status == "completed"
    execution = app.db.query_one(
        "SELECT result FROM tool_executions WHERE run_id=? AND tool='search_knowledge'",
        (result.run_id,),
    )
    result_body = json.loads(execution["result"])
    assert result_body["untrusted_sources"][0]["derived"] is True
    run = app.db.query_one("SELECT transcript FROM runs WHERE id=?", (result.run_id,))
    assert "<untrusted_tool_output nonce=" in run["transcript"]


def test_first_call_compaction_does_not_seed_excluded_taint(make_app, monkeypatch):
    from harness import transcript as transcript_mod

    app = make_app.build(default="ok")
    app.memory.remember(
        "tainted removed before first call", kind="fact", scope="global",
        origin="agent", provenance={"tainted": True, "tainted_origin": "inbox:x"},
    )
    original = transcript_mod.compact

    def remove_memory(messages, budget, tools_schema=None):
        prompt, repairs = original(messages, budget, tools_schema)
        prompt[0]["content"] = "# Current situation"
        return prompt, repairs

    monkeypatch.setattr(transcript_mod, "compact", remove_memory)
    result = app.runtime.submit_user_message("tainted removed before first call")
    assert result.status == "completed"
    run = app.db.query_one("SELECT meta FROM runs WHERE id=?", (result.run_id,))
    assert "tainted_sources" not in util.loads(run["meta"], {})


def test_core_profile_markers_are_unique_and_clipping_safe(make_app):
    app = make_app.build(default="ok")
    app.config.set("memory.core_profile_chars", 200)
    rows = []
    for suffix in ("alpha", "beta"):
        memory = app.memory.remember(
            "The user prefers the same opening prefix but different " + suffix,
            kind="preference", scope="global", origin="user",
        )["memory"]
        app.memory.set_pinned(memory["id"], True, changed_by="test")
        rows.append(memory)
    task = app.tasks.create(objective="core profile markers", origin="user")
    built = app.context.build(
        task=task, domain=None, trigger={"kind": "user"}, run_id="run_tags"
    )
    sources = [
        source for source in built["sources"]
        if source.get("section") == "core_profile"
    ]
    assert len(sources) == 2
    assert len({source["context_marker"] for source in sources}) == 2
    included = [source for source in sources if source["disposition"] == "included"]
    assert included
    for source in included:
        assert source["context_marker"] in built["system"]
    for source in sources:
        expected = 1 if source["disposition"] == "included" else 0
        assert app.memory.get(source["memory_id"])["access_count"] == expected


def test_consolidation_limits_do_not_let_tainted_inputs_crowd_out_clean(make_app):
    app = make_app.build(default="ok")
    clean_memory = app.memory.remember(
        "older clean memory", kind="fact", scope="global", origin="user"
    )["memory"]
    app.messages.record("user", "older clean message", source="web")
    for index in range(8):
        app.memory.remember(
            f"newer tainted memory {index}", kind="fact", scope="global",
            origin="agent", provenance={"tainted": True, "tainted_origin": "inbox:x"},
        )
        app.messages.record(
            "assistant", f"newer derived message {index}", source="agent",
            meta={"derived_untrusted": True, "tainted_origin": "inbox:x"},
        )
    app.config.set("consolidation.max_memories", 1)
    app.config.set("consolidation.max_messages", 1)
    app.model.scripts.append({"content": json.dumps({"changes": [
        {"op": "update", "memory_id": clean_memory["id"],
         "content": "clean memory updated", "reason": "cleanup"},
    ]})})

    report = app.consolidation.run(trigger="manual")
    assert report["status"] == "completed"
    assert report["skipped_untrusted"] >= 16
    prompt = app.model.calls[-1]["messages"][0]["content"]
    assert "older clean memory" in prompt
    assert "older clean message" in prompt
    assert "newer tainted memory" not in prompt
    assert "newer derived message" not in prompt



def test_compacted_same_task_history_does_not_seed_taint(make_app, monkeypatch):
    from harness import transcript as transcript_mod

    app = make_app.build(default="ok")
    task = app.tasks.create(objective="history compaction", origin="user")
    app.messages.record(
        "assistant", "derived history that will be clipped", source="agent",
        task_id=task["id"],
        meta={"derived_untrusted": True, "tainted_origin": "inbox:history"},
    )
    original = transcript_mod.compact

    def remove_history(messages, budget, tools_schema=None):
        prompt, repairs = original(messages, budget, tools_schema)
        prompt[0]["content"] = "# Current situation"
        return prompt, repairs

    monkeypatch.setattr(transcript_mod, "compact", remove_history)
    result = app.runtime.start_run(
        task, {"kind": "user"}, trigger_text="continue history compaction"
    )
    assert result.status == "completed"
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    assert "tainted_sources" not in util.loads(run["meta"], {})
    sources = util.loads(run["context_sources"], [])
    history = [source for source in sources if source.get("history_marker")]
    assert history and all(
        source["disposition"] == "excluded" for source in history
    )

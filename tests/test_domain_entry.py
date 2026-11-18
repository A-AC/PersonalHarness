"""Phase 10C: entering a domain delivers its environment and status."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from harness import util
from harness.modules.types import Module, ModuleBinding
from harness.tools import ToolSpec

REPO = Path(__file__).resolve().parents[1]


def _domain(
    app,
    domain_id: str,
    name: str,
    description: str,
    *,
    instructions: str = "",
):
    return app.domains.create(
        domain_id,
        name=name,
        description=description,
        instructions=instructions,
        created_by="test",
    )


def _domain_tool(app, module_id: str, tool_name: str, domain_id: str, **kwargs):
    app.tools.register(ToolSpec(
        name=tool_name,
        description=f"{tool_name} for {domain_id}",
        parameters={"type": "object", "properties": {}},
        risk=kwargs.pop("risk", "low"),
        handler=kwargs.pop(
            "handler", lambda ctx, args, module=module_id: {"module": module}
        ),
        module=module_id,
        scope="domain",
        **kwargs,
    ))
    app.modules.save(Module(
        id=module_id,
        kind="mcp",
        name=module_id,
        status="active",
        scope="domain",
        manifest={},
        origin="user",
        provenance={"source": "test"},
        created_by="test",
    ))
    app.modules.set_binding(ModuleBinding(
        module_id=module_id, domain_id=domain_id, enabled=True,
    ))


def _messages_for_task(app, task_id: str) -> list[dict]:
    return app.messages.for_task(task_id)


def test_context_has_domain_instructions_and_descriptions(make_app):
    app = make_app.build(default="ok")
    domain = _domain(
        app,
        "dom_health",
        "Health",
        "nutrition, exercise, and wellbeing",
        instructions="Ask about allergies before nutrition advice.",
    )
    task = app.tasks.create(
        objective="plan a workout", domain_id=domain["id"], origin="user"
    )

    built = app.context.build(
        task=task, domain=domain, trigger={"kind": "user"}
    )

    assert "# Domain instructions (Health)" in built["system"]
    assert "Ask about allergies before nutrition advice." in built["system"]
    assert "Available domains: " in built["system"]
    assert "Health (dom_health): nutrition, exercise, and wellbeing" in built["system"]
    assert any(
        source["ref"] == "domain:dom_health:instructions"
        and source["reason"] == "user-authored domain instructions"
        for source in built["sources"]
    )
    assert any(source["ref"] == "domain:list" for source in built["sources"])


def test_weak_routing_evidence_adds_bounded_hint(make_app):
    app = make_app.build(default="ok")
    _domain(app, "dom_clothing", "Clothing", "wardrobe choices")
    _domain(app, "dom_language", "Language learning", "practice a language")
    task = app.tasks.create(objective="what should I wear", origin="user")
    routing = {
        "domain_id": "general",
        "score": 0.0,
        "reason": "no domain evidence above threshold",
        "matched": [],
        "hint": {
            "domain_id": "dom_clothing",
            "name": "Clothing",
            "score": 0.5,
            "matched": ["wear", "weather"],
        },
        "candidates": [
            {
                "domain_id": "dom_clothing",
                "name": "Clothing",
                "score": 0.5,
                "breakdown": {"matched": ["wear", "weather"]},
            },
            {
                "domain_id": "dom_language",
                "name": "Language learning",
                "score": 0.5,
                "breakdown": {"matched": ["german"]},
            },
            {
                "domain_id": "dom_third",
                "name": "Third",
                "score": 0.5,
                "breakdown": {"matched": ["third"]},
            },
        ],
    }

    built = app.context.build(
        task=task, domain=None, trigger={"kind": "user"}, routing=routing
    )

    line = next(
        line for line in built["system"].splitlines()
        if line.startswith("Possible domain(s):")
    )
    assert "Clothing (matched: wear, weather)" in line
    assert "Language learning (matched: german)" in line
    assert "Third" not in line
    assert line.count("matched:") == 2
    assert any(
        source["ref"] == "domain:hint"
        and source["reason"] == "below-threshold domain evidence"
        for source in built["sources"]
    )

    quiet = app.context.build(
        task=task, domain=None, trigger={"kind": "user"},
        routing={
            "domain_id": "general", "candidates": [], "matched": [],
            "hint": None,
        },
    )
    assert "Possible domain(s):" not in quiet["system"]
    assert not any(source["ref"] == "domain:hint" for source in quiet["sources"])


def test_use_domain_briefing_and_recomputed_tools(make_app, tmp_path):
    app = make_app.build(scripts=[
        {"tool_calls": [{
            "name": "use_domain", "arguments": {"domain": "dom_new"},
        }]},
        {"content": "Finished in the new domain."},
    ])
    _domain(
        app,
        "dom_new",
        "New",
        "the newly entered work area",
        instructions="Use the new-domain checklist.",
    )
    _domain_tool(app, "mod_old", "old_exclusive_tool", "dom_old")
    _domain_tool(app, "mod_new", "new_exclusive_tool", "dom_new")
    app.memory.remember(
        "The launch checklist requires the blue badge.",
        scope="domain:dom_new",
        domains=app.domains,
    )
    skill_dir = tmp_path / "new-domain-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: New domain checklist\n"
        "description: Follow the new-domain launch checklist.\n"
        "domains: [dom_new]\n"
        "---\n"
        "Use the checklist.\n",
        encoding="utf-8",
    )
    app.modules.skills.import_dir(tmp_path)

    result = app.runtime.submit_user_message("do the work")
    assert result.status == "completed"
    assert app.tasks.get(result.task_id)["domain_id"] == "dom_new"

    assert len(app.model.calls) == 2
    next_tools = {
        item["function"]["name"] for item in app.model.calls[1]["tools"]
    }
    assert "new_exclusive_tool" in next_tools
    assert "old_exclusive_tool" not in next_tools

    next_prompt = json.dumps(app.model.calls[1]["messages"])
    assert "the newly entered work area" in next_prompt
    assert "Use the new-domain checklist." in next_prompt
    assert "The launch checklist requires the blue badge." in next_prompt
    assert "New domain checklist" in next_prompt
    assert "Follow the new-domain launch checklist." in next_prompt
    assert "new_exclusive_tool" in next_prompt

    events = [event["type"] for event in app.events.list()]
    assert "toolset.changed" in events
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    sources = util.loads(run["context_sources"], [])
    assert any(source["ref"] == "domain:dom_new:briefing" for source in sources)
    assert any(
        str(source["ref"]).startswith("memory:mem_") for source in sources
    )


def test_use_domain_rebuilds_system_context_for_new_domain(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{
            "name": "use_domain", "arguments": {"domain": "dom_new"},
        }]},
        {"content": "Finished in the new domain."},
    ])
    _domain(
        app,
        "dom_old",
        "Old",
        "the old work area",
        instructions="OLD DOMAIN INSTRUCTION",
    )
    _domain(
        app,
        "dom_new",
        "New",
        "the new work area",
        instructions="NEW DOMAIN INSTRUCTION",
    )
    task = app.tasks.create(
        objective="switch work areas",
        domain_id="dom_old",
        origin="user",
    )
    stale_routing = {
        "domain_id": "general",
        "score": 0.0,
        "reason": "weak route",
        "matched": [],
        "hint": {
            "domain_id": "dom_old",
            "name": "Old",
            "score": 0.8,
            "matched": ["work"],
        },
        "candidates": [],
    }

    result = app.runtime.start_run(task, {
        "kind": "user",
        "routing": stale_routing,
    })

    assert result.status == "completed"
    first_system = app.model.calls[0]["messages"][0]["content"]
    assert "OLD DOMAIN INSTRUCTION" in first_system
    assert "Active domain: Old" in first_system
    assert "Possible domain(s): Old" in first_system

    second_messages = app.model.calls[1]["messages"]
    second_system = next(
        message["content"] for message in second_messages
        if message["role"] == "system"
    )
    assert "NEW DOMAIN INSTRUCTION" in second_system
    assert "Active domain: New" in second_system
    assert "OLD DOMAIN INSTRUCTION" not in second_system
    assert "Active domain: Old" not in second_system
    assert "Possible domain(s):" not in second_system

    tool_result = next(
        message["content"] for message in second_messages
        if message["role"] == "tool"
    )
    assert "# Entering New" in tool_result
    assert "NEW DOMAIN INSTRUCTION" in tool_result

    events = [
        event for event in app.events.list(limit=100)
        if event["type"] == "domain.context_rebuilt"
    ]
    assert len(events) == 1
    assert events[0]["data"] == {
        "from_domain_id": "dom_old",
        "to_domain_id": "dom_new",
    }
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    sources = util.loads(run["context_sources"], [])
    assert any(
        source["ref"] == "domain:rebuild"
        and source["reason"] == "system prompt rebuilt after domain change"
        for source in sources
    )


def test_use_domain_to_general_rebuilds_without_old_instructions(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{
            "name": "use_domain", "arguments": {"domain": "general"},
        }]},
        {"content": "Back in General."},
    ])
    _domain(
        app,
        "dom_old",
        "Old",
        "the old work area",
        instructions="OLD DOMAIN INSTRUCTION",
    )
    task = app.tasks.create(
        objective="leave the old domain",
        domain_id="dom_old",
        origin="user",
    )

    result = app.runtime.start_run(task, {"kind": "user"})

    assert result.status == "completed"
    second_system = next(
        message["content"] for message in app.model.calls[1]["messages"]
        if message["role"] == "system"
    )
    assert "Active domain: General" in second_system
    assert "OLD DOMAIN INSTRUCTION" not in second_system
    assert "Active domain: Old" not in second_system


def test_misrouted_request_uses_hint_and_sees_domain_memory(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{
            "name": "use_domain", "arguments": {"domain": "dom_language"},
        }]},
        {"content": "German practice is ready."},
    ])
    _domain(
        app,
        "dom_language",
        "Language lab",
        "german practice and vocabulary",
    )
    app.memory.remember(
        "Vocabulary drills happen on Monday.",
        scope="domain:dom_language",
        domains=app.domains,
    )

    result = app.runtime.submit_user_message("learn german")

    assert result.status == "completed"
    first_prompt = json.dumps(app.model.calls[0]["messages"])
    assert "Possible domain(s): Language lab (matched: german)" in first_prompt
    next_prompt = json.dumps(app.model.calls[1]["messages"])
    assert "Vocabulary drills happen on Monday." in next_prompt
    assert app.tasks.get(result.task_id)["domain_id"] == "dom_language"
    routing = app.tasks.meta(result.task_id)["routing"]
    assert routing["domain_id"] == "general"
    assert routing["candidates"][0]["breakdown"]["matched"] == ["german"]


def test_tainted_domain_switch_still_escalates_side_effects(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{
            "name": "use_domain", "arguments": {"domain": "dom_new"},
        }]},
        {"tool_calls": [{
            "name": "new_side_effect",
            "arguments": {},
        }]},
    ])
    _domain(app, "dom_new", "New", "a domain with a state-changing tool")
    _domain_tool(
        app,
        "mod_new",
        "new_side_effect",
        "dom_new",
        side_effects=True,
        read_only=False,
    )
    task = app.tasks.create(
        objective="perform the state change", domain_id="general", origin="user"
    )

    result = app.runtime.start_run(task, {
        "kind": "user",
        "tainted_sources": [{
            "tool": "inbox",
            "module": "inbox",
            "origin": "inbox:mail",
        }],
    })

    assert result.status == "waiting_approval"
    approval = app.approvals.pending()[0]
    assert approval["tool"] == "new_side_effect"
    assert approval["risk"] == "confirm"
    assert "inbox:mail" in approval["reason"]
    assert "untrusted content" in approval["reason"]


def test_tool_discovery_ignores_inactive_modules(make_app):
    app = make_app.build(default="ok")
    _domain(app, "dom_fixture", "Fixture", "a domain with bundled tools")
    _domain_tool(app, "mod_active", "active_domain_tool", "dom_fixture")
    _domain_tool(app, "mod_disabled", "disabled_domain_tool", "dom_fixture")
    disabled = app.modules.get("mod_disabled")
    app.modules.save(Module(
        id=disabled.id,
        kind=disabled.kind,
        name=disabled.name,
        version=disabled.version,
        status="disabled",
        scope=disabled.scope,
        manifest=disabled.manifest,
        content_path=disabled.content_path,
        origin=disabled.origin,
        provenance=disabled.provenance,
        created_by=disabled.created_by,
        created_at=disabled.created_at,
        updated_at=disabled.updated_at,
        last_loaded_at=disabled.last_loaded_at,
        last_error=disabled.last_error,
        usage_count=disabled.usage_count,
    ))
    task = app.tasks.create(objective="general work", origin="user")
    selection = app.runtime._select_tools("general", "user")

    built = app.context.build(
        task=task,
        domain=app.domains.get("general"),
        trigger={"kind": "user"},
        tool_selection=selection,
    )

    discovery = next(
        line for line in built["system"].splitlines()
        if line.startswith("Tools in other domains:")
    )
    assert "active_domain_tool" in discovery
    assert "disabled_domain_tool" not in discovery


def test_tool_discovery_names_other_domain_tools_without_schemas(
    make_app, tmp_path
):
    import yaml

    app = make_app.build(default="ok")
    _domain(app, "dom_fixture", "Fixture", "a domain with bundled tools")
    server = tmp_path / "echo_server.py"
    server.write_text(
        (REPO / "examples" / "mcp" / "echo_server.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    config = tmp_path / "mcp.yaml"
    config.write_text(yaml.safe_dump({"servers": [{
        "name": "test_echo",
        "command": sys.executable,
        "args": [str(server)],
        "autoapprove": False,
        "domains": ["dom_fixture"],
    }]}), encoding="utf-8")
    app.config.set("tools.mcp_servers_file", str(config))
    app.modules.reload()

    task = app.tasks.create(objective="general work", origin="user")
    selection = app.runtime._select_tools("general", "user")
    built = app.context.build(
        task=task,
        domain=app.domains.get("general"),
        trigger={"kind": "user"},
        tool_selection=selection,
    )

    line = next(
        line for line in built["system"].splitlines()
        if line.startswith("Tools in other domains:")
    )
    assert "mcp_test_echo_echo" in line
    assert "Fixture" in line
    assert "parameters" not in line
    assert "properties" not in line
    assert len(line) <= 420
    assert any(
        source["ref"] == "domain:tool-discovery"
        and source["section"] == "tools"
        for source in built["sources"]
    )


def test_domain_status_is_recorded_once_and_can_be_silenced(make_app):
    app = make_app.build(default="ok")
    domain = _domain(app, "dom_health", "Health", "health work")
    task = app.tasks.create(
        objective="health work",
        domain_id=domain["id"],
        origin="user",
        meta={"notes": "", "routing": {
            "domain_id": "dom_health",
            "score": 1.5,
            "reason": "best domain evidence",
            "candidates": [],
            "matched": ["health"],
            "hint": None,
        }},
    )

    app.runtime.start_run(task, {"kind": "user"})
    app.runtime.resume_task(task, "continue", 99)

    entered = [
        event for event in app.events.list()
        if event["type"] == "domain.entered"
    ]
    assert len(entered) == 1
    assert entered[0]["data"]["matched"] == ["health"]
    notes = [
        message for message in _messages_for_task(app, task["id"])
        if (util.loads(message.get("meta") or "{}").get("kind")
            == "domain.status")
    ]
    assert len(notes) == 1
    assert notes[0]["content"] == "Entering Health - matched: health"
    assert util.loads(notes[0]["meta"])["chip"] is True

    app.config.set("domains.status_line", False)
    quiet_task = app.tasks.create(
        objective="quiet health work", domain_id=domain["id"], origin="user"
    )
    app.runtime.start_run(quiet_task, {"kind": "user"})
    entered = [
        event for event in app.events.list()
        if event["type"] == "domain.entered"
        and event["task_id"] == quiet_task["id"]
    ]
    assert len(entered) == 1
    quiet_notes = [
        message for message in _messages_for_task(app, quiet_task["id"])
        if (util.loads(message.get("meta") or "{}").get("kind")
            == "domain.status")
    ]
    assert quiet_notes == []


def test_subagent_inherits_current_parent_domain_after_switch(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{
            "name": "use_domain", "arguments": {"domain": "dom_new"},
        }]},
        {"tool_calls": [{
            "name": "delegate_task",
            "arguments": {"goal": "summarize the current domain"},
        }]},
        {"content": "Child summary."},
        {"content": "Parent summary."},
    ])
    _domain(app, "dom_new", "New", "the current parent domain")

    result = app.runtime.submit_user_message("delegate after entering")
    assert result.status == "completed"
    children = app.tasks.children(result.task_id)
    assert len(children) == 1
    child = children[0]
    assert child["domain_id"] == "dom_new"
    entered = [
        event for event in app.events.list()
        if event["type"] == "domain.entered"
        and event["task_id"] == child["id"]
    ]
    assert len(entered) == 1
    child_notes = [
        message for message in _messages_for_task(app, child["id"])
        if (util.loads(message.get("meta") or "{}").get("kind")
            == "domain.status")
    ]
    assert len(child_notes) == 1
    assert child_notes[0]["visible"] == 0


def test_route_preview_does_not_wait_for_the_runtime_lock(make_app):
    app = make_app.build(default="ok")
    _domain(
        app,
        "dom_health",
        "Health",
        "nutrition and exercise",
    )
    app.domains.update(
        "dom_health",
        keywords=["calorie", "nutrition"],
        examples=["How many calories are in this meal?"],
    )
    completed = threading.Event()
    result: dict = {}

    def preview():
        try:
            result["routing"] = app.runtime.preview_route(
                "How many calories are in this meal?"
            )
        finally:
            completed.set()

    assert app.runtime._lock.acquire(timeout=1)
    try:
        worker = threading.Thread(target=preview, daemon=True)
        worker.start()
        assert completed.wait(timeout=1), "route preview waited for the run lock"
    finally:
        app.runtime._lock.release()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert result["routing"]["domain_id"] == "dom_health"


def test_general_never_emits_domain_status(make_app):
    app = make_app.build(default="ok")
    task = app.tasks.create(objective="ordinary chatter", origin="user")
    app.runtime.start_run(task, {"kind": "user"})
    assert not any(
        event["type"] == "domain.entered" for event in app.events.list()
    )
    assert not any(
        util.loads(message.get("meta") or "{}").get("kind") == "domain.status"
        for message in _messages_for_task(app, task["id"])
    )

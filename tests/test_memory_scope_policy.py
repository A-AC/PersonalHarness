"""Task 2: memory scope policy for model-authored writes."""

from __future__ import annotations

from harness import util


def _domain(app, domain_id, name):
    return app.domains.create(domain_id, name=name, created_by="test")


def _run_remember(app, task, arguments):
    app.model.scripts.append({
        "tool_calls": [{"name": "remember", "arguments": arguments}]
    })
    app.model.scripts.append({"content": "saved"})
    return app.runtime.start_run(task, {"kind": "user"})


def _memory(app, content):
    return app.db.query_one(
        "SELECT * FROM memories WHERE content=? AND status='active'", (content,)
    )


def test_domain_task_remember_defaults_to_current_domain(make_app):
    app = make_app.build()
    _domain(app, "dom_health", "Health")
    task = app.tasks.create(
        objective="track my nutrition", domain_id="dom_health", origin="user"
    )
    _run_remember(app, task, {"content": "The user trains on Tuesdays"})
    row = _memory(app, "The user trains on Tuesdays")
    assert row is not None
    assert row["scope"] == "domain:dom_health"
    provenance = util.loads(row["provenance"], {})
    assert provenance["task_domain_id"] == "dom_health"
    assert provenance["task_domain"] == "Health"
    assert provenance["scope_choice"] == "narrow_domain"


def test_global_scope_is_allowed_but_records_choice_and_origin(make_app):
    app = make_app.build()
    _domain(app, "dom_health", "Health")
    task = app.tasks.create(
        objective="track my nutrition", domain_id="dom_health", origin="user"
    )
    _run_remember(
        app, task,
        {"content": "The user prefers metric units", "scope": "global"},
    )
    row = _memory(app, "The user prefers metric units")
    assert row["scope"] == "global"
    assert row["domain_id"] is None
    provenance = util.loads(row["provenance"], {})
    assert provenance["task_domain_id"] == "dom_health"
    assert provenance["scope_requested"] == "global"
    assert provenance["scope_effective"] == "global"
    assert provenance["scope_choice"] == "widen_global"


def test_preference_defaults_to_global_without_an_explicit_scope(make_app):
    app = make_app.build()
    _domain(app, "dom_health", "Health")
    task = app.tasks.create(
        objective="track my nutrition", domain_id="dom_health", origin="user"
    )
    _run_remember(
        app, task,
        {"content": "The user prefers morning workouts", "kind": "preference"},
    )
    row = _memory(app, "The user prefers morning workouts")
    assert row["scope"] == "global"
    provenance = util.loads(row["provenance"], {})
    assert provenance["scope_requested"] == "auto"
    assert provenance["scope_choice"] == "default_global"
    assert provenance["task_domain_id"] == "dom_health"


def test_other_domain_scope_is_coerced_to_the_task_domain(make_app):
    app = make_app.build()
    _domain(app, "dom_health", "Health")
    _domain(app, "dom_clothing", "Clothing")
    task = app.tasks.create(
        objective="track my nutrition", domain_id="dom_health", origin="user"
    )
    _run_remember(
        app, task,
        {"content": "Protein target is 120g", "scope": "domain:dom_clothing"},
    )
    row = _memory(app, "Protein target is 120g")
    assert row["scope"] == "domain:dom_health"
    assert row["domain_id"] == "dom_health"
    provenance = util.loads(row["provenance"], {})
    assert provenance["scope_requested"] == "domain:dom_clothing"
    assert provenance["scope_effective"] == "domain:dom_health"
    assert provenance["scope_choice"] == "narrow_domain"


def test_general_task_remember_is_global_and_keeps_general_origin(make_app):
    app = make_app.build()
    task = app.tasks.create(
        objective="remember a preference", domain_id="general", origin="user"
    )
    _run_remember(
        app, task, {"content": "The user likes tea", "scope": "domain:general"}
    )
    row = _memory(app, "The user likes tea")
    assert row["scope"] == "global"
    provenance = util.loads(row["provenance"], {})
    assert provenance["task_domain_id"] == "general"
    assert provenance["scope_effective"] == "global"


def test_subagent_child_keeps_its_inherited_domain(make_app):
    app = make_app.build()
    _domain(app, "dom_health", "Health")
    child = app.tasks.create(
        objective="research protein sources", domain_id="dom_health",
        origin="subagent", role="subagent",
    )
    _run_remember(app, child, {"content": "Greek yogurt has protein"})
    row = _memory(app, "Greek yogurt has protein")
    assert row["scope"] == "domain:dom_health"
    provenance = util.loads(row["provenance"], {})
    assert provenance["task_domain_id"] == "dom_health"


def test_remember_after_use_domain_uses_the_new_domain(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{
            "name": "use_domain", "arguments": {"domain": "dom_clothing"},
        }]},
        {"tool_calls": [{
            "name": "remember", "arguments": {"content": "The winter coat is in the hall closet"},
        }]},
        {"content": "done"},
    ])
    _domain(app, "dom_health", "Health")
    _domain(app, "dom_clothing", "Clothing")
    task = app.tasks.create(
        objective="switch areas", domain_id="dom_health", origin="user"
    )
    result = app.runtime.start_run(task, {"kind": "user"})
    assert result.status == "completed"
    row = _memory(app, "The winter coat is in the hall closet")
    assert row["scope"] == "domain:dom_clothing"
    provenance = util.loads(row["provenance"], {})
    assert provenance["task_domain_id"] == "dom_clothing"


def test_approval_resumed_remember_uses_the_task_domain(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{
            "name": "remember",
            "arguments": {"content": "Approved domain fact"},
        }]},
        {"content": "done"},
    ])
    _domain(app, "dom_health", "Health")
    task = app.tasks.create(
        objective="remember a fact", domain_id="dom_health", origin="user",
        meta={"tainted_sources": [{
            "tool": "web_search", "module": "web",
            "origin": "https://example.test", "at": util.iso(),
        }]},
    )
    result = app.runtime.start_run(task, {"kind": "user"})
    assert result.status == "waiting_approval"
    approval = app.approvals.pending()[0]
    app.approvals.decide(approval["id"], "approve", decided_by="web")
    app.runtime.resume_after_approval(approval, "web")
    row = _memory(app, "Approved domain fact")
    assert row is not None
    assert row["scope"] == "domain:dom_health"
    provenance = util.loads(row["provenance"], {})
    assert provenance["task_domain_id"] == "dom_health"
    assert provenance["tainted"] is True


def test_consolidation_scope_is_not_a_model_task_write(make_app):
    app = make_app.build()
    _domain(app, "dom_health", "Health")
    result = app.memory.remember(
        "Consolidated fact", scope="global", origin="consolidation",
        provenance={"changed_by": "consolidation"}, domains=app.domains,
    )
    row = result["memory"]
    assert row["scope"] == "global"
    assert "scope_choice" not in row["provenance"]

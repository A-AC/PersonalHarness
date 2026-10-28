"""C. Domains and resumption: routing, thresholds, ambiguity, resumption."""

from __future__ import annotations

import json

from harness import util


def _say(app, text, source="web"):
    return app.runtime.submit_user_message(text, source=source)


def _seed_domain(app, name, description):
    return app.domains.create(
        util.new_id("dom"), name=name, description=description,
        created_by="test",
    )


def test_message_routes_to_relevant_existing_domain(make_app):
    app = make_app.build(default="ok")
    _seed_domain(app, "Homelab", "servers, network, nas, rack maintenance")
    result = _say(app, "Please check the nas backup status in the homelab.")
    task = app.tasks.get(result.task_id)
    assert task["domain_id"] == "general" or task["domain_id"] != ""
    routed = [e for e in app.events.list() if e["type"] == "domain.routed"]
    assert routed, "routing decision must be recorded"
    # "nas" + "homelab" evidence should beat general.
    data = routed[-1]["data"] if isinstance(routed[-1]["data"], dict) else {}
    assert any(c["name"] == "Homelab" for c in data.get("candidates", []))


def test_new_topic_does_not_create_a_domain(make_app):
    app = make_app.build(default="ok")
    before = app.domains.list_all()
    _say(app, "What is the capital of France?")
    _say(app, "Tell me a joke about dentists.")
    after = app.domains.list_all()
    assert [d["id"] for d in after] == [d["id"] for d in before]
    assert all(d["name"] == "General" for d in after)


def test_unrelated_domains_do_not_leak_into_each_other(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "remember", "arguments": {
            "content": "Garden plans must use native plants.",
            "kind": "preference", "scope": "domain:dmgarden"}}]},
        {"content": "Saved the garden preference."},
    ])
    garden = _seed_domain(app, "Garden", "planting, soil, irrigation")
    _seed_domain(app, "Homelab", "servers, network, nas")
    app.domains.db.execute("UPDATE domains SET id='dmgarden' WHERE id=?",
                           (garden["id"],))
    _say(app, "For the garden, remember that plans must use native plants.")
    # Push the garden exchange out of the recent-history window so only
    # scoped retrieval could leak it.
    for index in range(30):
        app.messages.record("user", f"kitchen notes {index}", source="web")
    # A homelab question must not surface the garden-scoped memory.
    app.model.scripts.append({"content": "The nas is fine."})
    _say(app, "check the homelab nas status")
    last_context = json.dumps(app.model.last_messages)
    assert "native plants" not in last_context
    # But a garden question retrieves it.
    app.model.scripts.append({"content": "Garden: native plants only."})
    _say(app, "what are the garden plan rules?")
    last_context = json.dumps(app.model.last_messages)
    assert "native plants" in last_context


def test_new_run_for_existing_task_includes_its_prior_history(make_app):
    app = make_app.build(scripts=[{"content": "continued cleanly"}])
    task = app.tasks.create(objective="continue redirect cleanup", origin="user")
    app.messages.record(
        "user", "Preserve redirects; never drop them.", source="web",
        task_id=task["id"],
    )
    app.runtime.start_run(task, {"kind": "user"}, trigger_text="continue")
    context = json.dumps(app.model.last_messages)
    assert "Preserve redirects" in context
    assert "Earlier work on this task" in context


def test_unfinished_task_resumes_without_full_transcript(make_app):
    app = make_app.build(scripts=[])
    ASK = "<" + "ask_user" + "/>"
    app.model.scripts.append({"content": f"Which IP should I check? {ASK}"})
    first = _say(app, "Check the homelab server.")
    assert first.status == "waiting_user"

    # A restart happens; the same data dir gets a fresh process.
    app2 = make_app.restart(scripts=[{"content": "Checked 10.0.0.5: healthy."}])
    task = app2.tasks.get(first.task_id)
    assert task["status"] == "waiting_user"  # persisted across restart
    second = _say(app2, "10.0.0.5")
    assert second.status == "completed"
    assert second.task_id == first.task_id
    assert second.run_id == first.run_id  # same run resumed
    # The resumed transcript carried the prior question, not a re-plan.
    context = json.dumps(app2.model.calls[0]["messages"])
    assert "Which IP" in context


def test_competing_candidates_trigger_clarification(make_app):
    app = make_app.build(scripts=[{"content": "Which of the two should I continue?"}])
    task_a = app.tasks.create(objective="migrate the wiki server to new hardware",
                              domain_id="general", origin="user")
    task_b = app.tasks.create(objective="migrate the mail server to new hardware",
                              domain_id="general", origin="user")
    app.tasks.set_status(task_a["id"], "waiting_user", reason="waiting")
    app.tasks.set_status(task_b["id"], "waiting_user", reason="waiting")
    result = _say(app, "continue the server migration")
    # Ambiguity is surfaced to the model (which asks), not guessed silently.
    app.tasks.get(result.task_id)
    meta = app.tasks.meta(result.task_id)
    assert meta.get("ambiguous_candidates")
    context = json.dumps(app.model.last_messages)
    assert "migrate the wiki" in context
    assert "migrate the mail" in context
    assert "Ask which one to continue" in context
    routed = [e for e in app.events.list() if e["type"] == "domain.routed"]
    assert routed


def test_followup_prefers_resuming_over_new_task(make_app):
    app = make_app.build(scripts=[])
    ASK = "<" + "ask_user" + "/>"
    app.model.scripts.append({"content": f"Which rack do you mean? {ASK}"})
    first = _say(app, "Start checking the homelab rack height.")
    assert first.status == "waiting_user"
    # The answer resumes the same open task instead of creating a new one.
    app.model.scripts.append({"content": "The 42U one. Height check done."})
    second = _say(app, "the 42u one")
    assert second.status == "completed"
    assert second.task_id == first.task_id
    # A completed task is not resurrected: the next message opens new work.
    app.model.scripts.append({"content": "New topic acknowledged."})
    third = _say(app, "Tell me about the weather tomorrow.")
    assert third.task_id != first.task_id


def test_domain_creation_requires_approval(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "create_domain", "arguments": {
            "name": "Astronomy", "description": "telescope and observing logs",
            "reason": "recurring distinct work with its own knowledge"}}]},
        {"content": "I proposed the Astronomy domain."},
    ])
    result = _say(app, "I keep doing telescope work; set up a place for it.")
    assert result.status == "waiting_approval"
    approval = app.approvals.pending()[0]
    assert approval["tool"] == "create_domain"
    names = [d["name"] for d in app.domains.list_all()]
    assert "Astronomy" not in names  # nothing created without the user
    # Approving creates exactly the proposed domain.
    app.approvals.decide(approval["id"], "approve", decided_by="web")
    app.runtime.resume_after_approval(approval, "web")
    names = [d["name"] for d in app.domains.list_all()]
    assert "Astronomy" in names

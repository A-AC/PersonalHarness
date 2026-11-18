"""Phase 10E UI contracts for domains, routing evidence, and core profile."""

from __future__ import annotations

from harness import util


def _health_domain(app):
    return app.domains.create(
        "dom_ui_health",
        name="Health",
        description="nutrition and exercise",
        keywords=["calorie", "nutrition"],
        examples=["How many calories are in this meal?"],
        instructions="Prefer concise nutrition guidance.",
        created_by="test",
    )


def test_domain_ui_assets_wire_routing_and_core_profile(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)

    script = http.get("/assets/app.js")
    assert script.status_code == 200
    source = script.text
    assert "/api/domains/route-preview" in source
    assert "keywords" in source and "examples" in source and "instructions" in source
    assert "routing_stats" in source and "routed_here" in source
    assert "corrected_away" in source and "corrected_in" in source
    assert "domainchip" in source
    assert "details" in source and "why" in source
    assert "core" in source and "Core profile" in source
    assert "suggested_keywords" in source
    assert "moved from the task list" in source
    assert "task.domain_id" in source
    assert "corrected_from" in source
    assert "Corrected from:" in source
    assert "openSuggestionIds" in source
    assert "openSuggestionIds.has" in source
    assert "/api/domain-suggestions" in source
    assert "memory.core_profile_chars" in source
    assert "not injected" in source

    style = http.get("/assets/style.css")
    assert style.status_code == 200
    assert ".domainchip" in style.text
    assert ".why" in style.text


def test_domains_api_cards_include_counts_and_routing_stats(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    domain = _health_domain(app)

    response = http.get("/api/domains")
    assert response.status_code == 200
    cards = {item["id"]: item for item in response.json()["domains"]}
    card = cards[domain["id"]]
    assert set(card["counts"]) == {
        "memories", "skills", "modules", "tasks", "last_used",
    }
    assert set(card["routing_stats"]) == {
        "routed_here", "corrected_away", "corrected_in",
    }


def test_routed_turn_exposes_task_routing_and_status_chip(make_app, client):
    app = make_app.build(default="ok")
    _health_domain(app)
    http = client(app)

    response = http.post("/api/message", json={
        "text": "How many calories are in this meal?",
    })
    assert response.status_code == 200
    task_id = response.json()["task_id"]

    detail = http.get(f"/api/tasks/{task_id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["routing"]["domain_id"] == "dom_ui_health"
    assert payload["routing"]["candidates"]
    assert payload["domain_entered"]["data"]["domain_id"] == "dom_ui_health"
    assert payload["domain_entered"]["ts"]

    messages = http.get("/api/messages").json()["messages"]
    status_notes = [
        message for message in messages
        if message.get("meta") and util.loads(message["meta"]).get("kind")
        == "domain.status"
    ]
    assert len(status_notes) == 1
    meta = util.loads(status_notes[0]["meta"])
    assert meta["chip"] is True
    assert meta["domain_id"] == "dom_ui_health"
    assert status_notes[0]["content"].startswith("Entering Health")


def test_summary_working_task_includes_routing_evidence(make_app, client):
    app = make_app.build(default="ok")
    domain = _health_domain(app)
    routing = {
        "domain_id": domain["id"],
        "score": 2.5,
        "reason": "best domain evidence",
        "candidates": [],
        "matched": ["calorie"],
        "hint": None,
    }
    app.tasks.create(
        objective="track calories",
        domain_id=domain["id"],
        origin="user",
        meta={"notes": "", "routing": routing},
    )

    response = client(app).get("/api/summary")
    assert response.status_code == 200
    working = response.json()["working_task"]
    assert working is not None
    assert working["routing"] == routing

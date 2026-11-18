"""Phase 10G: deterministic domain suggestions from repeated General work."""

from __future__ import annotations

import datetime as dt

from harness import util
from harness.__main__ import build_parser, cmd_domains


CORPUS = [
    "Plan the vegetable garden layout",
    "Prepare soil for the vegetable garden",
    "Research companion plants for my vegetable garden",
    "Order seeds for the vegetable garden",
    "Design a watering schedule for the vegetable garden",
    "Repair the vegetable garden compost bin",
]


def _add_general_tasks(app, objectives=CORPUS):
    return [
        app.tasks.create(
            objective=objective,
            domain_id="general",
            origin="user",
        )
        for objective in objectives
    ]


def _suggestion_notes(app):
    notes = []
    for message in app.messages.list_visible(limit=100):
        meta = util.loads(message.get("meta") or "{}", {})
        if meta.get("kind") == "domain.suggestion":
            notes.append((message, meta))
    return notes


def test_fixed_corpus_creates_one_suggestion_and_one_visible_note(make_app):
    app = make_app.build(default="ok")
    tasks = _add_general_tasks(app)

    report = app.domain_suggestions.scan()

    assert report["status"] == "completed"
    assert len(report["created"]) == 1
    suggestion = report["created"][0]
    assert suggestion["status"] == "open"
    assert set(suggestion["terms"]) == {"vegetable", "garden"}
    assert set(suggestion["task_ids"]) == {task["id"] for task in tasks}
    assert suggestion["name"]

    notes = _suggestion_notes(app)
    assert len(notes) == 1
    message, meta = notes[0]
    assert message["role"] == "note"
    assert message["source"] == "system"
    assert message["visible"] == 1
    assert message["task_id"] is None
    assert meta == {
        "kind": "domain.suggestion",
        "suggestion_id": suggestion["id"],
        "terms": suggestion["terms"],
        "task_ids": suggestion["task_ids"],
        "name": suggestion["name"],
    }
    assert suggestion["name"] in message["content"]
    assert "vegetable" in message["content"]
    assert "6" in message["content"]

    # A second pass without new work does not duplicate the suggestion/note.
    second = app.domain_suggestions.scan()
    assert second["created"] == []
    assert len(app.domain_suggestions.list()) == 1
    assert len(_suggestion_notes(app)) == 1

    events = [
        event for event in app.events.list(limit=100)
        if event["type"] == "domain.suggestion"
    ]
    assert len(events) == 1
    assert events[0]["data"]["id"] == suggestion["id"]
    assert events[0]["data"]["task_count"] == 6


def test_suggestion_uses_surface_terms_and_noisy_cluster_association(make_app):
    app = make_app.build(default="ok")
    for index in range(6):
        app.tasks.create(
            objective=(
                "Practice cello exercises"
                + (" with soil notes" if index < 5 else "")
            ),
            domain_id="general",
            origin="user",
        )

    suggestion = app.domain_suggestions.scan()["created"][0]

    assert "exercises" in suggestion["terms"]
    assert "exercis" not in suggestion["terms"]
    assert suggestion["name"] == "Practice Cello Exercises"
    # soil appears in 5/6 tasks, below min_tasks but above the association
    # threshold for this cluster.
    assert "soil" in suggestion["terms"]
    note = _suggestion_notes(app)[0][0]
    assert "Practice cello exercises" in note["content"]


def test_min_tasks_threshold_requires_six_tasks_by_default(make_app):
    app = make_app.build(default="ok")
    _add_general_tasks(app, CORPUS[:5])

    report = app.domain_suggestions.scan()

    assert report["created"] == []
    assert app.domain_suggestions.list() == []
    assert _suggestion_notes(app) == []


def test_scan_examines_only_the_most_recent_bounded_task_window(make_app):
    app = make_app.build(default="ok")
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    old_ids = []
    recent_ids = []
    with app.db.transaction() as conn:
        for index in range(306):
            task_id = f"task_suggestions_{index:03d}"
            created_at = util.iso(base + dt.timedelta(minutes=index))
            if index < 6:
                objective = "legacyapi retrofit"
                old_ids.append(task_id)
            elif index >= 300:
                objective = "apiculture hive"
                recent_ids.append(task_id)
            else:
                # Every filler task has unique vocabulary, so it cannot form a
                # cluster by itself.
                objective = f"filler{index} uniquevocab{index}"
            conn.execute(
                "INSERT INTO tasks(id, domain_id, objective, status, origin,"
                " created_at, updated_at, last_activity_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    task_id, "general", objective, "active", "user",
                    created_at, created_at, created_at,
                ),
            )

    report = app.domain_suggestions.scan(
        base + dt.timedelta(days=1)
    )

    assert report["examined"] == 300
    assert len(report["created"]) == 1
    suggestion = report["created"][0]
    assert "apiculture" in suggestion["terms"]
    assert set(suggestion["task_ids"]) == set(recent_ids)
    assert not set(suggestion["task_ids"]).intersection(old_ids)


def test_active_domain_coverage_prevents_a_suggestion(make_app):
    app = make_app.build(default="ok")
    app.domains.create(
        "vegetables",
        name="Vegetables",
        description="garden planning",
        keywords=["vegetable", "garden"],
        created_by="test",
    )
    _add_general_tasks(app)

    report = app.domain_suggestions.scan()

    assert report["created"] == []
    assert app.domain_suggestions.list() == []


def test_dismissed_cluster_is_remembered_when_more_tasks_arrive(make_app):
    app = make_app.build(default="ok")
    _add_general_tasks(app)
    first = app.domain_suggestions.scan()["created"][0]
    dismissed = app.domain_suggestions.dismiss(first["id"])
    assert dismissed["status"] == "dismissed"
    assert app.domain_suggestions.dismiss(first["id"])["status"] == "dismissed"

    _add_general_tasks(app, [
        "Improve the vegetable garden path",
        "Label vegetables in the garden",
    ])
    report = app.domain_suggestions.scan()

    assert report["created"] == []
    assert len(app.domain_suggestions.list(status="all")) == 1
    assert app.domain_suggestions.get(first["id"])["status"] == "dismissed"
    assert len(_suggestion_notes(app)) == 1


def test_scan_creates_at_most_one_suggestion_per_tick(make_app):
    app = make_app.build(default="ok")
    _add_general_tasks(app, [
        "Practice cello scales for the cello recital",
        "Record cello rehearsal progress",
        "Order cello sheet music",
        "Practice cello rhythm exercises",
        "Plan the cello recital programme",
        "Record cello practice notes",
    ])
    _add_general_tasks(app, [
        "Mix sourdough starter for bread",
        "Shape sourdough bread loaves",
        "Bake sourdough bread at home",
        "Score sourdough bread crust",
        "Store sourdough bread for later",
        "Plan sourdough bread recipes",
    ])

    report = app.domain_suggestions.scan()

    assert len(report["created"]) <= 1
    assert len(app.domain_suggestions.list()) == len(report["created"])
    assert len(_suggestion_notes(app)) == len(report["created"])


def test_api_lists_and_dismisses_suggestions_with_task_context(
    make_app, client,
):
    app = make_app.build(default="ok")
    tasks = _add_general_tasks(app)
    suggestion = app.domain_suggestions.scan()["created"][0]
    http = client(app)

    listing = http.get("/api/domain-suggestions")
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["count"] == 1
    row = payload["suggestions"][0]
    assert row["id"] == suggestion["id"]
    assert isinstance(row["terms"], list)
    assert isinstance(row["task_ids"], list)
    assert row["task_count"] == 6
    assert row["tasks"][0]["objective"]
    assert len(row["task_objectives"]) == 6

    dismissed = http.post(
        f"/api/domain-suggestions/{suggestion['id']}/dismiss"
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["suggestion"]["status"] == "dismissed"
    assert http.get("/api/domain-suggestions").json()["count"] == 0
    # The persisted chat note remains, but it is no longer an open suggestion.
    assert len(_suggestion_notes(app)) == 1
    assert http.post("/api/domain-suggestions/missing/dismiss").status_code == 404


def test_cli_lists_open_domain_suggestions(monkeypatch, capsys):
    class FakeClient:
        def __init__(self, base: str, token: str):
            self.base = base
            self.token = token

        def call(self, method: str, path: str, body=None):
            assert method == "GET"
            assert path == "/api/domain-suggestions"
            return {
                "count": 1,
                "suggestions": [{
                    "id": "dsug_cli",
                    "name": "Vegetable Garden",
                    "terms": ["vegetable", "garden"],
                    "task_count": 6,
                }],
            }

    monkeypatch.setattr("harness.__main__.Client", FakeClient)
    args = build_parser().parse_args(["domains", "--suggestions"])
    args.data_dir = "unused"
    args.url = "http://unused"
    args.token = "token"

    cmd_domains(args)

    output = capsys.readouterr().out
    assert "Vegetable Garden" in output
    assert "dsug_cli" in output
    assert "vegetable, garden" in output
    assert "tasks: 6" in output


def test_domains_payload_includes_open_suggestions(make_app, client):
    app = make_app.build(default="ok")
    _add_general_tasks(app)
    suggestion = app.domain_suggestions.scan()["created"][0]

    response = client(app).get("/api/domains")

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["suggestions"]] == [suggestion["id"]]
    assert payload["domains"]


def test_create_domain_with_suggestion_id_marks_suggestion_created(
    make_app, client,
):
    app = make_app.build(default="ok")
    _add_general_tasks(app)
    suggestion = app.domain_suggestions.scan()["created"][0]

    response = client(app).post("/api/domains", json={
        "name": suggestion["name"],
        "description": "Created from a usage suggestion.",
        "keywords": suggestion["terms"],
        "suggestion_id": suggestion["id"],
    })

    assert response.status_code == 200
    assert response.json()["domain"]["name"] == suggestion["name"]
    assert app.domain_suggestions.get(suggestion["id"])["status"] == "created"
    assert any(
        event["type"] == "domain.suggestion.created"
        for event in app.events.list(limit=100)
    )


def test_unknown_suggestion_id_does_not_block_explicit_domain_creation(
    make_app, client,
):
    app = make_app.build(default="ok")

    response = client(app).post("/api/domains", json={
        "name": "Explicit Domain",
        "suggestion_id": "dsug_does_not_exist",
    })

    assert response.status_code == 200
    assert response.json()["domain"]["name"] == "Explicit Domain"
    assert app.domains.by_name("Explicit Domain") is not None


def test_heartbeat_reports_suggestions_and_survives_scan_failure(
    make_app, monkeypatch,
):
    app = make_app.build(default="ok")
    _add_general_tasks(app)

    report = app.scheduler.heartbeat()
    assert report["suggestions"]["status"] == "completed"
    assert len(report["suggestions"]["created"]) == 1

    def broken_scan(now=None):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(app.domain_suggestions, "scan", broken_scan)
    failed = app.scheduler.heartbeat()
    assert failed["suggestions"]["status"] == "failed"
    assert "scan exploded" in failed["suggestions"]["error"]
    # The failed pass did not prevent the rest of the heartbeat from running.
    assert "skills" in failed
    assert app.db.meta_get("scheduler.last_tick")


def test_fresh_app_has_no_suggestion_rows_and_scan_creates_nothing(make_app):
    app = make_app.build(default="ok")

    assert app.domain_suggestions.list() == []
    report = app.domain_suggestions.scan()
    assert report["status"] == "completed"
    assert report["created"] == []
    assert report["examined"] == 0
    assert app.domain_suggestions.list() == []
    # No stock domain was added: General remains the only default domain.
    assert [domain["id"] for domain in app.domains.list_all()] == ["general"]


def test_ui_assets_wire_suggestion_create_and_dismiss(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)

    script = http.get("/assets/app.js").text
    assert "/api/domain-suggestions" in script
    assert "suggestion_id" in script
    assert "Create domain" in script
    assert "Dismiss" in script
    assert ".domainsuggestion" in script
    assert "openSuggestionIds.has(suggestionId)" in script
    assert "suggestionJob = api('/api/domain-suggestions')" in script

    style = http.get("/assets/style.css").text
    assert ".domainsuggestion" in style

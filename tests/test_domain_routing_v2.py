"""Phase 10B: deterministic routing evidence v2 and learned vocabulary."""

from __future__ import annotations

from pathlib import Path

import yaml

from harness import util
from harness.app import build_app
from harness.evaluation.fixtures import apply_fixtures
from harness.model import ScriptedModel
from harness.evaluation.routing import routing_metrics


ROOT = Path(__file__).resolve().parents[1]
ROUTING_CORPUS = ROOT / "eval" / "scenarios" / "k_routing.yaml"

CORPUS_DOMAINS = [
    {
        "name": "Health",
        "description": (
            "health, fitness, nutrition, calories, protein, diet, sleep, "
            "exercise, doctor, symptoms"
        ),
        "keywords": [
            "nutrition", "calories", "protein", "diet", "exercise", "sleep",
            "fitness", "healthy",
        ],
        "examples": [
            "how many calories are in this",
            "is this healthy",
            "how much protein is in this",
        ],
    },
    {
        "name": "Home automation",
        "description": (
            "smart home, lights, thermostat, heating, scenes, sensors, automation"
        ),
        "keywords": [
            "lights", "thermostat", "heating", "scene", "sensor", "automation",
            "porch", "kitchen",
        ],
        "examples": ["turn off the lights", "set the thermostat"],
    },
    {
        "name": "Language learning",
        "description": (
            "language study, Spanish, French, vocabulary, grammar, flashcards, "
            "practice"
        ),
        "keywords": [
            "spanish", "french", "german", "italian", "vocabulary", "grammar",
            "flashcards", "pronunciation",
        ],
        "examples": ["how do I say this in Spanish", "quiz me on French verbs"],
    },
    {
        "name": "Clothing",
        "description": "clothing, wardrobe, outfits, wear, weather, dress, shoes, jacket",
        "keywords": [
            "outfit", "wear", "shirt", "shoes", "jacket", "wardrobe", "dress",
            "coat",
        ],
        "examples": ["what should I wear", "does this match my outfit"],
    },
]

GENERAL_CHATTER = [
    "What is the capital of France?",
    "Write a python function to reverse a list",
    "Draft a polite email to my landlord about the leak",
    "Why is my car engine making a ticking noise?",
    "Split the meetup budget between six people",
    "Who won the world cup in 2022?",
    "What time is it in Tokyo right now?",
    "Summarize these meeting notes into three bullets",
]


def _corpus_cases() -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    data = yaml.safe_load(ROUTING_CORPUS.read_text(encoding="utf-8")) or {}
    scenarios = data.get("scenarios") or []
    cases: list[dict[str, str]] = []
    for scenario in scenarios:
        if str(scenario.get("id") or "").startswith("k_routing.learned_"):
            continue
        steps = scenario.get("steps") or []
        if len(steps) != 1:
            continue
        step = steps[0]
        routing = (step.get("expect") or {}).get("routing") or {}
        if not routing.get("domain"):
            continue
        cases.append({
            "id": str(scenario["id"]),
            "text": str(step["input"]),
            "expected": str(routing["domain"]),
            "heldout": "heldout" in (scenario.get("tags") or []),
        })
    return cases, list(scenarios[0]["fixtures"]["domains"])


def test_nutrition_routes_with_keywords_and_examples(make_app):
    app = make_app.build(default="ok")
    app.domains.create(
        "health", name="Health", description="food and wellbeing",
        created_by="test",
        keywords=["nutrition", "calories", "protein"],
        examples=[
            "nutritional information for a meal",
            "how many calories are in this",
        ],
    )

    for text in (
        "nutritional info for chicken tikka masala",
        "calories in a big mac",
        "protein in greek yogurt",
    ):
        routing = app.domains.route(text)
        assert routing["domain_id"] == "health", (text, routing)


def test_description_only_domain_routes_when_description_has_enough_evidence(make_app):
    app = make_app.build(default="ok")
    app.domains.create(
        "health", name="Health",
        description="nutrition information for chicken meals",
        created_by="test",
    )

    routing = app.domains.route("nutritional info for chicken tikka masala")
    assert routing["domain_id"] == "health"
    assert routing["breakdown"]["signals"]["description"]["terms"] == [
        "chicken", "nutrition",
    ]


def test_two_learned_memory_terms_win_without_explicit_domain_terms(make_app):
    app = make_app.build(default="ok")
    domain = app.domains.create(
        "journal", name="Journal", description="", created_by="test",
    )
    app.memory.remember(
        "My calorie and protein targets for tomorrow are 1800 and 120.",
        kind="fact",
        scope=f"domain:{domain['id']}",
        domain_id=domain["id"],
        domains=app.domains,
    )

    result = app.runtime.submit_user_message(
        "How many calories and protein tomorrow?"
    )
    task = app.tasks.get(result.task_id)
    assert task["domain_id"] == domain["id"]
    assert app.tasks.meta(task["id"])["routing"]["confidence"] == "medium"


def test_one_past_task_term_does_not_route_by_itself(make_app):
    app = make_app.build(default="ok")
    domain = app.domains.create(
        "baking", name="Baking", description="", created_by="test",
    )
    app.tasks.create(
        objective="banana bread plan",
        domain_id=domain["id"],
        origin="user",
    )

    routing = app.domains.route("banana")
    assert routing["domain_id"] == "general"


def test_only_validated_task_affinity_becomes_routing_evidence(make_app):
    app = make_app.build(default="ok")
    domain = app.domains.create(
        "baking", name="Baking", description="", created_by="test",
    )
    task = app.tasks.create(
        objective="banana bread plan", domain_id=domain["id"], origin="user",
        meta={"routing": {
            "domain_id": domain["id"], "reason": "best domain evidence",
        }},
    )
    assert app.domains.route("banana bread")["domain_id"] == "general"
    meta = app.tasks.meta(task["id"])
    meta["routing"]["corrected_to"] = domain["id"]
    app.tasks.set_meta(task["id"], meta)
    assert app.domains.route("banana bread")["domain_id"] == domain["id"]


def test_two_memory_terms_route_a_keywordless_domain(make_app):
    app = make_app.build(default="ok")
    domain = app.domains.create(
        "fermentation", name="Fermentation", description="", created_by="test",
    )
    app.memory.remember(
        "sourdough starter feeding schedule",
        kind="fact",
        scope=f"domain:{domain['id']}",
        domain_id=domain["id"],
        domains=app.domains,
    )

    routing = app.runtime.preview_route("feed my sourdough starter")
    assert routing["domain_id"] == domain["id"]
    assert routing["breakdown"]["signals"]["learned"]["contribution"] >= 1.0


def test_recent_message_overlap_does_not_route(make_app):
    app = make_app.build(default="ok")
    domain = app.domains.create(
        "hobbies", name="Hobbies", description="", created_by="test",
    )

    routing = app.domains.route(
        "knitting",
        recent_messages=[{
            "domain_id": domain["id"],
            "content": "knitting patterns",
        }],
    )
    assert routing["domain_id"] == "general"


def test_one_skill_term_does_not_route_but_two_do(make_app):
    app = make_app.build(default="ok")
    domain = app.domains.create(
        "needlework", name="Needlework", description="", created_by="test",
    )
    manager = app.modules.skills
    one_term = manager.create_draft(
        name="knitting",
        description="a practical guide",
        domains=[domain["id"]],
        body="Review the project.",
    )
    manager.activate(one_term.id)

    one = app.runtime.preview_route("knitting")
    assert one["domain_id"] == "general"

    two_terms = manager.create_draft(
        name="knitting patterns",
        description="a practical guide",
        domains=[domain["id"]],
        body="Review the project.",
    )
    manager.activate(two_terms.id)

    two = app.runtime.preview_route("knitting patterns")
    assert two["domain_id"] == domain["id"]
    assert two["breakdown"]["signals"]["learned"]["contribution"] == 1.0


def test_explicit_keyword_evidence_still_routes(make_app):
    app = make_app.build(default="ok")
    app.domains.create(
        "fitness", name="Fitness", description="", created_by="test",
        keywords=["workout"],
    )

    routing = app.domains.route("Plan my workout")
    assert routing["domain_id"] == "fitness"
    assert routing["breakdown"]["signals"]["keyword"]["contribution"] == 1.0


def test_general_chatter_never_selects_a_specific_domain(make_app):
    app = make_app.build(default="ok")
    apply_fixtures(app, {"domains": CORPUS_DOMAINS})

    for text in GENERAL_CHATTER:
        routing = app.domains.route(text)
        assert routing["domain_id"] == "general", (text, routing)


def test_real_use_generic_overlap_does_not_self_reinforce_auto_routes(make_app):
    app = make_app.build(default="ok")
    health = app.domains.create(
        "dom_health", name="health",
        description=(
            "A domain for health, nutrition, gym, medicine, and overall "
            "wellness topics where I can help with information, planning, "
            "and guidance."
        ),
        created_by="test",
        keywords=["health", "nutrition", "gym", "medicine", "wellness"],
        examples=["help me plan my nutrition", "gym workout ideas"],
    )
    for objective in (
        'Can you help me create an "Office day" calendar event',
        "I will be in a testing phase for a bit of time",
        "in the calendar... I have no use for it here",
    ):
        app.tasks.create(
            objective=objective, domain_id=health["id"], origin="user",
            meta={"routing": {
                "domain_id": health["id"], "reason": "best domain evidence",
            }},
        )

    for text in (
        "I will probably be in a testing phase for a bit of time",
        "No... change the name of the Office day event",
        "in the calendar... I have no use for it here",
        "Can you help me create two events in my calendar",
    ):
        routing = app.runtime.preview_route(text)
        assert routing["domain_id"] == "general", (text, routing)
        health_candidate = next((
            item for item in routing["candidates"]
            if item["domain_id"] == health["id"]
        ), None)
        if health_candidate is not None:
            assert not health_candidate["breakdown"]["auto_route"]
        assert app.context._routing_hint(routing) == ""


def test_domain_routed_event_contains_per_term_breakdown(make_app):
    app = make_app.build(default="ok")
    app.domains.create(
        "fitness", name="Fitness", description="training",
        created_by="test", keywords=["workout"],
    )

    app.runtime.submit_user_message("Plan my workouts")
    events = [
        event for event in app.events.list(limit=100)
        if event["type"] == "domain.routed"
    ]
    assert events
    data = events[-1]["data"]
    candidate = next(
        item for item in data["candidates"]
        if item["domain_id"] == "fitness"
    )
    assert candidate["signals"]["keyword"]["terms"] == ["workout"]
    assert candidate["signals"]["keyword"]["contribution"] == 1.0
    assert "matched_terms" not in candidate["signals"]["keyword"]
    assert candidate["matched"] == ["workouts"]
    assert data["breakdown"]["signals"]["keyword"]["terms"] == ["workout"]
    assert data["breakdown"]["matched"] == ["workouts"]
    assert data["matched"] == ["workouts"]
    assert "signals" not in data


def test_routing_hint_uses_original_surface_terms(make_app):
    app = make_app.build(default="ok")
    app.domains.create(
        "training", name="Training", description="workout", created_by="test",
    )

    routing = app.domains.route("Plan my workouts")
    assert routing["domain_id"] == "general"
    assert routing["hint"]["matched"] == ["workouts"]


def test_routing_never_creates_a_domain(make_app):
    app = make_app.build(default="ok")
    app.domains.create(
        "health", name="Health", description="nutrition",
        created_by="test", keywords=["calories"],
    )
    before = [domain["id"] for domain in app.domains.list_all()]

    app.domains.route("How many calories are in this meal?")
    app.runtime.submit_user_message("calories in a big mac")

    after = [domain["id"] for domain in app.domains.list_all()]
    assert after == before


def test_create_domain_approval_carries_keywords_and_examples(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "create_domain", "arguments": {
            "name": "Astronomy",
            "description": "telescope and observing logs",
            "reason": "recurring distinct work",
            "keywords": ["astronomy", "telescope"],
            "examples": ["log tonight's observation"],
        }}]},
        {"content": "Astronomy is ready."},
    ])
    result = app.runtime.submit_user_message("Set up an astronomy workspace")
    assert result.status == "waiting_approval"
    approval = app.approvals.pending()[0]
    assert approval["arguments"]["keywords"] == ["astronomy", "telescope"]
    assert approval["arguments"]["examples"] == ["log tonight's observation"]

    app.approvals.decide(approval["id"], "approve", decided_by="web")
    app.runtime.resume_after_approval(approval, "web")
    domain = app.domains.by_name("Astronomy")
    assert domain["keywords"] == ["astronomy", "telescope"]
    assert domain["examples"] == ["log tonight's observation"]


def test_create_domain_missing_keywords_fails_before_approval(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "create_domain", "arguments": {
            "name": "Astronomy",
            "description": "telescope and observing logs",
            "reason": "recurring distinct work",
            "examples": ["log tonight's observation"],
        }}]},
        {"content": "The proposal needs routing terms."},
    ])
    result = app.runtime.submit_user_message("Set up an astronomy workspace")
    assert result.status == "completed"
    assert app.approvals.pending() == []
    assert app.domains.by_name("Astronomy") is None
    row = app.db.query_one(
        "SELECT transcript FROM runs WHERE id=?", (result.run_id,)
    )
    transcript = util.loads(row["transcript"], [])
    outcomes = [
        util.loads(message.get("content") or "", {})
        for message in transcript
        if message.get("role") == "tool"
    ]
    assert any(
        outcome.get("status") == "denied"
        and "keywords" in str(outcome.get("error") or "")
        for outcome in outcomes
    )


def test_create_domain_empty_keywords_fails_before_approval(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "create_domain", "arguments": {
            "name": "Astronomy",
            "description": "telescope and observing logs",
            "reason": "recurring distinct work",
            "keywords": [],
            "examples": ["log tonight's observation"],
        }}]},
        {"content": "The proposal needs routing terms."},
    ])
    result = app.runtime.submit_user_message("Set up an astronomy workspace")
    assert result.status == "completed"
    assert app.approvals.pending() == []
    assert app.domains.by_name("Astronomy") is None
    row = app.db.query_one(
        "SELECT transcript FROM runs WHERE id=?", (result.run_id,)
    )
    transcript = util.loads(row["transcript"], [])
    outcomes = [
        util.loads(message.get("content") or "", {})
        for message in transcript
        if message.get("role") == "tool"
    ]
    assert any(
        outcome.get("status") == "denied"
        and "keywords" in str(outcome.get("error") or "")
        for outcome in outcomes
    )


def test_create_domain_empty_examples_fails_before_approval(make_app):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "create_domain", "arguments": {
            "name": "Astronomy",
            "description": "telescope and observing logs",
            "reason": "recurring distinct work",
            "keywords": ["astronomy"],
            "examples": [],
        }}]},
        {"content": "The proposal needs example phrases."},
    ])
    result = app.runtime.submit_user_message("Set up an astronomy workspace")
    assert result.status == "completed"
    assert app.approvals.pending() == []
    assert app.domains.by_name("Astronomy") is None
    row = app.db.query_one(
        "SELECT transcript FROM runs WHERE id=?", (result.run_id,)
    )
    transcript = util.loads(row["transcript"], [])
    outcomes = [
        util.loads(message.get("content") or "", {})
        for message in transcript
        if message.get("role") == "tool"
    ]
    assert any(
        outcome.get("status") == "denied"
        and "examples" in str(outcome.get("error") or "")
        for outcome in outcomes
    )


def test_memory_domain_affinity_is_read_only_and_stems_matches(make_app):
    app = make_app.build(default="ok")
    domain = app.domains.create(
        "health", name="Health", created_by="test",
    )
    memory = app.memory.remember(
        "Track calories in every meal.",
        kind="fact", scope=f"domain:{domain['id']}",
        domain_id=domain["id"], domains=app.domains,
    )["memory"]
    access_count = int(memory["access_count"])
    event_count = len(app.events.list(limit=100))

    affinity = app.memory.domain_affinity("How many calories are in this?")
    assert affinity == {domain["id"]: ["calorie"]}
    assert app.memory.get(memory["id"])["access_count"] == access_count
    assert len(app.events.list(limit=100)) == event_count


def test_learned_affinity_respects_disabled_stemming(make_app):
    app = make_app.build(default="ok")
    app.config.set("domains.stemming", False)
    domain = app.domains.create(
        "health", name="Health", created_by="test",
    )
    app.memory.remember(
        "Track calories in every meal.",
        kind="fact", scope=f"domain:{domain['id']}",
        domain_id=domain["id"], domains=app.domains,
    )
    manager = app.modules.skills
    skill = manager.create_draft(
        name="calories coach",
        description="track calories intake",
        domains=[domain["id"]],
        body="Review the daily calories log.",
    )
    manager.activate(skill.id)

    assert app.memory.domain_affinity("How many calories are in this?") == {
        domain["id"]: ["calories"]
    }
    assert manager.domain_affinity("How many calories did I eat?") == {
        domain["id"]: ["calories"]
    }


def test_skill_domain_affinity_uses_active_enabled_bindings(make_app):
    app = make_app.build(default="ok")
    domain = app.domains.create(
        "health", name="Health", created_by="test",
    )
    manager = app.modules.skills
    skill = manager.create_draft(
        name="calorie coach",
        description="track calorie intake",
        domains=[domain["id"]],
        body="Review the daily calorie log.",
    )
    manager.activate(skill.id)

    affinity = manager.domain_affinity("How many calories did I eat?")
    assert affinity == {domain["id"]: ["calorie"]}

    manager.disable(skill.id)
    assert manager.domain_affinity("How many calories did I eat?") == {}


def test_domain_api_accepts_routing_fields_and_reports_validation_errors(
    make_app, client,
):
    app = make_app.build(default="ok")
    http = client(app)
    created = http.post("/api/domains", json={
        "name": "API Health",
        "description": "nutrition",
        "keywords": ["calorie"],
        "examples": ["calorie check"],
        "instructions": "Prefer whole foods.",
    })
    assert created.status_code == 200
    domain = created.json()["domain"]
    assert domain["keywords"] == ["calorie"]
    assert domain["examples"] == ["calorie check"]
    assert domain["instructions"] == "Prefer whole foods."

    updated = http.patch(f"/api/domains/{domain['id']}", json={
        "keywords": "protein",
        "examples": ["protein target"],
        "instructions": "Track protein.",
    })
    assert updated.status_code == 200
    assert updated.json()["domain"]["keywords"] == ["protein"]
    assert updated.json()["domain"]["examples"] == ["protein target"]

    invalid = http.patch(f"/api/domains/{domain['id']}", json={
        "keywords": [123],
    })
    assert invalid.status_code == 400


def test_shipped_defaults_meet_routing_corpus_targets(make_app):
    app = make_app.build(default="ok")
    cases, domains = _corpus_cases()
    # One-turn scenarios across the shipped environment. Multi-turn
    # follow-ups are measured by the semantic bake-off, which runs the
    # preceding turns into conversation state.
    assert len(cases) >= 55
    apply_fixtures(app, {"domains": domains})

    expected: list[str] = []
    predicted: list[str] = []
    for case in cases:
        if case["heldout"]:
            continue
        routing = app.domains.route(case["text"])
        record = app.domains.get(routing["domain_id"])
        expected.append(case["expected"])
        predicted.append(str(record["name"]) if record else routing["domain_id"])

    metrics = routing_metrics(expected, predicted)
    assert metrics["accuracy"] >= 0.85, metrics
    assert metrics["false_positive_rate"] <= 0.05, metrics


def test_message_fixture_records_visible_non_system_message(make_app):
    app = make_app.build(default="ok")

    created = apply_fixtures(app, {
        "domains": [{"name": "Astronomy"}],
        "messages": [{
            "role": "user",
            "content": "telescope collimation tips",
            "domain": "Astronomy",
        }],
    })

    assert len(created["messages"]) == 1
    message = app.messages.get(created["messages"][0])
    assert message["role"] == "user"
    assert message["content"] == "telescope collimation tips"
    assert message["domain_id"] == created["domains"]["Astronomy"]
    assert message["visible"] == 1


def test_learned_routing_scenarios_match_their_labels(tmp_path):
    data = yaml.safe_load(ROUTING_CORPUS.read_text(encoding="utf-8")) or {}
    scenarios = [
        scenario for scenario in data.get("scenarios") or []
        if str(scenario.get("id") or "").startswith("k_routing.learned_")
    ]
    assert len(scenarios) == 4

    for scenario in scenarios:
        app = build_app(
            tmp_path / str(scenario["id"]),
            model=ScriptedModel(default="ok"),
        )
        try:
            apply_fixtures(app, scenario.get("fixtures") or {})
            app.config.set("domains.model_routing", "off")
            step = scenario["steps"][0]
            routing = app.runtime.preview_route(str(step["input"]))
            record = app.domains.get(str(routing["domain_id"]))
            predicted = (
                str(record["name"]) if record else str(routing["domain_id"])
            )
            expected = str(step["expect"]["routing"]["domain"])
            assert predicted == expected, (scenario["id"], routing)
        finally:
            app.shutdown()

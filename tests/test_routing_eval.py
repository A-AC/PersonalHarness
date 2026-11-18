"""Routing and cross-domain-recall eval assets (Phase 10A)."""

from __future__ import annotations

from pathlib import Path

from harness.evaluation.cli import gating_failures, measurement_failures
from harness.evaluation.loader import load_scenarios
from harness.evaluation.models import ScenarioResult
from harness.evaluation.routing import (
    best_grid_point,
    confusion_table,
    routing_metrics,
)
from scripts import route_sweep

ROOT = Path(__file__).resolve().parents[1]
ROUTING = ROOT / "eval" / "scenarios" / "k_routing.yaml"
MEMORY = ROOT / "eval" / "scenarios" / "k_memory_recall.yaml"

EXPECTED_DOMAINS = {
    "Health", "Home automation", "Language learning", "Clothing", "General",
    "Fermentation", "Astronomy",
}
METRIC_DOMAINS = {
    "Health", "Home automation", "Language learning", "Clothing", "General",
}


def test_routing_corpus_loads_and_is_labelled() -> None:
    scenarios = load_scenarios(ROUTING)
    assert len(scenarios) >= 40
    assert {scenario.category for scenario in scenarios} == {"routing"}
    for scenario in scenarios:
        assert len(scenario.steps) == 1, scenario.id
        step = scenario.steps[0]
        assert step.kind == "turn", scenario.id
        assert step.input.strip(), scenario.id
        domain = (step.expect.get("routing") or {}).get("domain")
        assert domain in EXPECTED_DOMAINS, scenario.id
        assert scenario.settings.get("domains", {}).get(
            "model_routing"
        ) == "off", scenario.id
        assert scenario.fixtures.get("domains"), scenario.id
        assert scenario.measurement is True, scenario.id
    assert sum(
        1 for scenario in scenarios
        if (scenario.steps[0].expect["routing"]["domain"] == "General")
    ) >= 12


def test_routing_metrics_and_confusion_are_deterministic() -> None:
    expected = [
        "Health", "General", "Clothing", "Health",
        "Home automation", "General", "Language learning", "Clothing",
    ]
    predicted = [
        "Health", "General", "Health", "General",
        "General", "Clothing", "Language learning", "Language learning",
    ]
    first = routing_metrics(expected, predicted)
    second = routing_metrics(expected, predicted)
    assert first == second
    assert route_sweep.routing_metrics(expected, predicted) == first
    assert first["total"] == 8
    assert first["correct"] == 3
    assert first["accuracy"] == 0.375
    assert first["general_fallback_count"] == 3
    assert first["general_fallback_rate"] == 0.375
    assert first["false_positive_count"] == 3
    assert first["false_positive_rate"] == 0.375
    assert first["misses"] == 2
    assert first["confusion"] == confusion_table(expected, predicted)

    table = first["confusion"]
    assert table["labels"] == sorted(METRIC_DOMAINS)
    assert table["rows"]["Health"]["Health"] == 1
    assert table["rows"]["Health"]["General"] == 1
    assert table["rows"]["General"]["Clothing"] == 1
    assert table["rows"]["Clothing"]["Language learning"] == 1


def test_confusion_table_folds_case_and_whitespace() -> None:
    table = confusion_table(["Health", "health "], ["health", "HEALTH"])
    assert table["labels"] == ["Health"]
    assert table["rows"]["Health"]["Health"] == 2


def test_measurement_failures_do_not_gate() -> None:
    def result(measurement: bool, status: str = "failed") -> ScenarioResult:
        return ScenarioResult(
            scenario_id="s", name="s", category="routing",
            severity="high", mode="deterministic", repeat=1, run_id="r",
            status=status, measurement=measurement,
        )

    scenarios = [result(True), result(False), result(True, "passed")]
    assert len(measurement_failures(scenarios)) == 1
    assert len(gating_failures(scenarios)) == 1
    assert not gating_failures([result(True)])


def test_routing_sweep_is_deterministic_and_has_a_general_control() -> None:
    cases, domains = route_sweep.load_corpus(ROUTING)
    first = route_sweep.sweep(
        cases, domains, [1.0], [1.0], tag="deterministic"
    )
    second = route_sweep.sweep(
        cases, domains, [1.0], [1.0], tag="deterministic"
    )
    assert first == second
    assert first["experiment"] == "deterministic"
    general_expected = sum(
        1 for case in cases if case["expected"].strip().lower() == "general"
    )
    control = first["control"]
    assert control["total"] == len(cases)
    assert control["correct"] == general_expected
    assert control["accuracy"] == round(general_expected / len(cases), 4)


def test_best_grid_point_tie_break() -> None:
    grid = [
        {"accuracy": 0.5, "threshold": 1.0, "margin": 0.5},
        {"accuracy": 0.4, "threshold": 3.0, "margin": 0.5},
        {"accuracy": 0.5, "threshold": 2.0, "margin": 1.0},
        {"accuracy": 0.5, "threshold": 2.0, "margin": 0.5},
    ]
    best = best_grid_point(grid)
    assert best == {"accuracy": 0.5, "threshold": 2.0, "margin": 0.5}
    assert route_sweep.best_grid_point(grid) == best


def test_fixture_reapplication_updates_existing_domain_evidence(make_app) -> None:
    from harness.evaluation.fixtures import apply_fixtures

    app = make_app.build(default="ok")
    apply_fixtures(app, {"domains": [{
        "name": "Health",
        "description": "first description",
        "keywords": ["calorie"],
        "examples": ["how many calories"],
        "instructions": "first instructions",
    }]})
    apply_fixtures(app, {"domains": [{
        "name": "Health",
        "description": "updated description",
        "keywords": ["protein", "sleep"],
        "examples": ["how much protein"],
        "instructions": "updated instructions",
    }]})
    domain = app.domains.by_name("Health")
    assert domain["description"] == "updated description"
    assert domain["keywords"] == ["protein", "sleep"]
    assert domain["examples"] == ["how much protein"]
    assert domain["instructions"] == "updated instructions"


def test_memory_recall_corpus_loads() -> None:
    scenarios = load_scenarios(MEMORY)
    assert 6 <= len(scenarios) <= 8
    for scenario in scenarios:
        assert scenario.category == "memory", scenario.id
        assert len(scenario.steps) == 1, scenario.id
        assert scenario.steps[0].input.strip(), scenario.id
        context = scenario.steps[0].expect.get("context") or {}
        assert context.get("system_contains"), scenario.id
        assert context.get("system_not_contains"), scenario.id
        names = {
            item.get("name")
            for item in scenario.fixtures.get("domains") or []
        }
        assert {"Health", "Clothing"} <= names, scenario.id
        memories = scenario.fixtures.get("memories") or []
        assert any(item.get("domain") == "Health" for item in memories)
        assert any(item.get("domain") == "Clothing" for item in memories)
        assert any(item.get("scope") == "global" for item in memories)

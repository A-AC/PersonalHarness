"""Deterministic bake-off plumbing over a small routing subset."""

from __future__ import annotations

from pathlib import Path

from harness.__main__ import build_parser
from harness.evaluation.bakeoff import run_bakeoff
from harness.evaluation.loader import load_scenarios, select_scenarios

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "eval" / "scenarios" / "k_routing.yaml"


def test_bakeoff_deterministic_reports_every_strategy(tmp_path):
    scenarios = select_scenarios(
        load_scenarios(CORPUS),
        ids="k_routing.health_calories,k_routing.spanish_calories,"
            "k_routing.followup_health_dinner",
    )
    payload = run_bakeoff(
        scenarios, ["lexical", "embedding", "llm", "hybrid"],
        mode="deterministic", results_dir=tmp_path,
        tag="bakeoff_test",
    )
    assert payload["scenario_count"] == 3
    results = {item["strategy"]: item for item in payload["results"]}
    assert set(results) == {"lexical", "embedding", "llm", "hybrid"}
    for result in results.values():
        assert result["available"] is True
        assert result["plumbing_only"] is True
        assert result["splits"]["base"]["total"] >= 1
        assert result["splits"]["heldout"]["total"] >= 1
    assert (tmp_path / "bakeoff_test.json").is_file()
    assert (tmp_path / "bakeoff_test.md").is_file()


def test_eval_parser_exposes_bakeoff_flags():
    args = build_parser().parse_args([
        "eval", "--routing-bakeoff", "--strategies", "lexical,embedding",
    ])
    assert args.routing_bakeoff is True
    assert args.strategies == "lexical,embedding"

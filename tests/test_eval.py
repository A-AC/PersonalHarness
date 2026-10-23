from __future__ import annotations

from pathlib import Path

from harness.eval import SCENARIOS, run_suite, select_scenarios, summarize
from harness.model.scripted import ScriptedAdapter
from tests.conftest import plan, respond


def test_scenario_ids_are_unique_and_selectable() -> None:
    ids = [item.id for item in SCENARIOS]
    assert len(ids) == len(set(ids))
    assert select_scenarios("injection")[0].id == "injection_echo"
    assert {item.id for item in select_scenarios("respond")} >= {"respond_role", "respond_greeting"}


def test_summarize_reports_pass_rates_by_category() -> None:
    results = [
        {
            "id": "a",
            "category": "one",
            "repeat": 1,
            "passed": True,
            "failures": [],
            "turns": [
                {
                    "traces": 2,
                    "trace_validation": [{"ok": True}, {"ok": False}],
                    "attempts": [{"status": "succeeded"}],
                    "latency_ms": [10, 20],
                    "task_states": [{"state": "completed"}],
                }
            ],
        },
        {
            "id": "a",
            "category": "one",
            "repeat": 2,
            "passed": False,
            "failures": ["broken"],
            "turns": [
                {
                    "traces": 1,
                    "trace_validation": [{"ok": True}],
                    "attempts": [{"status": "failed"}],
                    "latency_ms": [30],
                    "task_states": [{"state": "failed"}],
                }
            ],
        },
    ]
    summary = summarize(results)
    assert summary["runs"] == 2
    assert summary["passed"] == 1
    assert summary["by_category"]["one"] == [1, 2]
    assert summary["by_scenario"]["a"] == [1, 2]
    assert summary["proposal_validity"] == 2 / 3
    assert summary["invalid_proposals"] == 1
    assert summary["capability_calls"] == 2
    assert summary["failed_capability_calls"] == 1
    assert summary["turn_latency_ms_mean"] == 20


def test_run_suite_uses_isolated_harnesses_and_writes_results(tmp_path: Path) -> None:
    scenario = select_scenarios("respond_role")[0]

    def factory(_settings):
        return ScriptedAdapter([plan("role"), respond("I help with tracked tasks.")])

    output = run_suite(
        data_dir=tmp_path,
        repeats=2,
        scenarios=[scenario],
        tag="unit",
        adapter_factory=factory,
    )
    assert output["summary"]["runs"] == 2
    assert output["summary"]["passed"] == 2
    assert (tmp_path / "eval" / "results" / "unit.json").exists()
    assert (tmp_path / "eval" / "unit" / scenario.id / "repeat_1" / "settings.yaml").exists()
    assert (tmp_path / "eval" / "unit" / scenario.id / "repeat_2").exists()

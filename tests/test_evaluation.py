"""Evaluation framework tests: loading, safety, execution, reports, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from harness.__main__ import main
from harness.evaluation import cli as cli_mod
from harness.evaluation import runner as runner_mod
from harness.evaluation.aggregate import (
    aggregate_reports,
    load_reports,
    render_aggregate,
)
from harness.evaluation.compare import compare_reports, render_comparison
from harness.evaluation.loader import (
    ScenarioError,
    load_scenarios,
    select_scenarios,
)
from harness.evaluation.models import ScenarioStep
from harness.evaluation.quality import evaluate_quality
from harness.evaluation.report import load_run, render_markdown, summary_lines
from harness.evaluation.runner import (
    EndpointUnavailable,
    Evaluator,
    load_base_config,
    run_evaluation,
)
from harness.evaluation.safety import (
    MARKER,
    SandboxError,
    prepare_sandbox,
)

PLAIN_SCENARIO = {
    "scenarios": [
        {
            "id": "unit_plain",
            "name": "A plain turn completes",
            "category": "unit",
            "severity": "high",
            "modes": ["deterministic"],
            "steps": [
                {
                    "kind": "turn",
                    "name": "say hello",
                    "input": "hello",
                    "script": [{"content": "Hello from the eval."}],
                    "expect": {
                        "status": "completed",
                        "response": {"contains": "Hello"},
                        "task": {"status": "completed"},
                    },
                }
            ],
        }
    ]
}


def _write_scenarios(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


# -- loading and selection ---------------------------------------------------


def test_loader_reads_yaml_and_selects(tmp_path: Path) -> None:
    path = _write_scenarios(tmp_path / "one.yaml", PLAIN_SCENARIO)
    scenarios = load_scenarios(path)
    assert [scenario.id for scenario in scenarios] == ["unit_plain"]
    assert select_scenarios(scenarios, mode="deterministic") == scenarios
    assert select_scenarios(scenarios, ids="unit_*") == scenarios
    assert select_scenarios(scenarios, category="unit") == scenarios
    with pytest.raises(ScenarioError):
        select_scenarios(scenarios, mode="live")
    with pytest.raises(ScenarioError):
        select_scenarios(scenarios, ids="nope")


def test_loader_rejects_duplicates_and_unknown_steps(tmp_path: Path) -> None:
    duplicate = {"scenarios": PLAIN_SCENARIO["scenarios"] * 2}
    _write_scenarios(tmp_path / "dup.yaml", duplicate)
    with pytest.raises(ScenarioError, match="duplicate scenario id"):
        load_scenarios(tmp_path)

    bad = {
        "scenarios": [
            {
                "id": "bad_step",
                "steps": [{"kind": "teleport", "input": "x"}],
            }
        ]
    }
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    _write_scenarios(bad_dir / "bad.yaml", bad)
    with pytest.raises(ScenarioError, match="unknown kind"):
        load_scenarios(bad_dir)


def test_shipped_scenario_suite_is_mode_partitioned() -> None:
    scenarios = load_scenarios(Path(__file__).resolve().parents[1] / "eval"
                               / "scenarios")
    ids = {scenario.id for scenario in scenarios}
    assert len(scenarios) >= 53
    assert "a_plain_reply" in ids
    assert "g_live_plain_reply" in ids
    assert "h_loop_remember_then_search" in ids
    assert "i_live_multi_tool_loop" in ids
    assert "j_live_three_bullets" in ids
    live = select_scenarios(scenarios, mode="live")
    deterministic = select_scenarios(scenarios, mode="deterministic")
    assert live and all(scenario.modes == ["live"] for scenario in live)
    assert deterministic and all(
        "live" not in scenario.modes for scenario in deterministic
    )
    assert "i_live_multi_tool_loop" in {scenario.id for scenario in live}
    assert "j_live_unknown_fact" in {scenario.id for scenario in live}
    assert "h_loop_remember_then_search" in {
        scenario.id for scenario in deterministic
    }


# -- sandbox safety ----------------------------------------------------------


def test_sandbox_refuses_protected_and_unmarked_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    with pytest.raises(SandboxError, match="protected"):
        prepare_sandbox(repo, repo_root=repo)
    with pytest.raises(SandboxError, match="protected"):
        prepare_sandbox(repo / "data", repo_root=repo)
    with pytest.raises(SandboxError, match="protected"):
        prepare_sandbox(repo / "scratch", repo_root=repo)

    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "keep.txt").write_text("user file", encoding="utf-8")
    with pytest.raises(SandboxError, match="non-empty"):
        prepare_sandbox(dirty, repo_root=repo)
    with pytest.raises(SandboxError, match="unmarked"):
        prepare_sandbox(dirty, repo_root=repo, reset=True)

    (dirty / MARKER).write_text("marker", encoding="utf-8")
    assert prepare_sandbox(dirty, repo_root=repo) == dirty.resolve()
    prepare_sandbox(dirty, repo_root=repo, reset=True)
    assert (dirty / MARKER).is_file()
    assert not (dirty / "keep.txt").exists()


# -- deterministic execution and reports ------------------------------------


def test_deterministic_run_writes_reports(tmp_path: Path) -> None:
    scenarios = load_scenarios(
        _write_scenarios(tmp_path / "scenarios.yaml", PLAIN_SCENARIO)
    )
    results = tmp_path / "results"
    run = run_evaluation(
        scenarios,
        mode="deterministic",
        sandbox=tmp_path / "sandbox",
        results_dir=results,
        tag="unit",
        environ={},
    )

    assert run.scenarios[0].status == "passed"
    assert run.results_path == str(results / "unit.json")
    assert Path(run.results_path).is_file()
    assert Path(run.report_path).is_file()
    report = load_run("unit", results)
    assert report["tag"] == "unit"
    assert report["metrics"]["scenario_pass_rate"]["value"] == 1.0
    assert "unit_plain" in Path(run.report_path).read_text(encoding="utf-8")
    assert any("scenarios 1/1" in line for line in summary_lines(report))


def test_markdown_lists_failures(tmp_path: Path) -> None:
    scenarios = load_scenarios(
        _write_scenarios(tmp_path / "scenarios.yaml", PLAIN_SCENARIO)
    )
    run = run_evaluation(
        scenarios, mode="deterministic", sandbox=tmp_path / "sandbox",
        results_dir=tmp_path / "results", tag="md", environ={},
    )
    run.scenarios[0].status = "failed"
    run.scenarios[0].steps[0].assertions[0].status = "failed"
    text = render_markdown(run)
    assert "## Failures" in text
    assert "unit_plain" in text


# -- tool loop assertions ----------------------------------------------------


def _loop_scenario(sequence: list[dict]) -> dict:
    return {
        "scenarios": [
            {
                "id": "unit_tool_loop",
                "name": "A scripted tool loop",
                "category": "unit",
                "modes": ["deterministic"],
                "steps": [
                    {
                        "kind": "turn",
                        "input": "loop",
                        "script": [
                            {"tool_calls": [{"name": "remember", "arguments": {
                                "content": "alpha fact", "scope": "global"}}]},
                            {"tool_calls": [{"name": "search_knowledge",
                                             "arguments": {"query": "alpha"}}]},
                            {"content": "alpha fact"},
                        ],
                        "expect": {
                            "status": "completed",
                            "tools": {
                                "calls": [
                                    {"tool": "remember", "status": "succeeded",
                                     "result_contains": "alpha"},
                                    {"tool": "search_knowledge",
                                     "status": "succeeded",
                                     "result_contains": "alpha"},
                                ],
                                "sequence": sequence,
                            },
                        },
                    }
                ],
            }
        ]
    }


def test_tool_result_and_sequence_assertions(tmp_path: Path) -> None:
    good = _loop_scenario([{"tool": "remember"},
                           {"tool": "search_knowledge"}])
    scenarios = load_scenarios(
        _write_scenarios(tmp_path / "good.yaml", good)
    )
    run = run_evaluation(
        scenarios, mode="deterministic", sandbox=tmp_path / "sandbox",
        results_dir=tmp_path / "results", tag="good", environ={},
    )
    assert run.scenarios[0].status == "passed"

    bad = _loop_scenario([{"tool": "search_knowledge"},
                          {"tool": "remember"}])
    bad["scenarios"][0]["id"] = "unit_bad_sequence"
    scenarios = load_scenarios(_write_scenarios(tmp_path / "bad.yaml", bad))
    run = run_evaluation(
        scenarios, mode="deterministic", sandbox=tmp_path / "sandbox2",
        results_dir=tmp_path / "results", tag="bad", environ={},
    )
    assert run.scenarios[0].status == "failed"
    assert any(
        assertion.id == "s1.tools.sequence"
        and assertion.status == "failed"
        for assertion in run.scenarios[0].assertions
    )


def test_batch_deferral_is_observable(tmp_path: Path) -> None:
    data = {
        "scenarios": [{
            "id": "unit_deferred",
            "name": "Only the first batch action runs",
            "category": "unit",
            "modes": ["deterministic"],
            "steps": [{
                "kind": "turn",
                "input": "save both",
                "script": [
                    {"tool_calls": [
                        {"name": "remember", "arguments": {
                            "content": "alpha note", "scope": "global"}},
                        {"name": "remember", "arguments": {
                            "content": "beta note", "scope": "global"}},
                    ]},
                    {"content": "first only"},
                ],
                "expect": {
                    "status": "completed",
                    "memories": {"none": [{"content_contains": "beta"}]},
                    "tools": {"count_max": 1, "deferred_min": 1},
                },
            }],
        }]
    }
    scenarios = load_scenarios(
        _write_scenarios(tmp_path / "deferred.yaml", data)
    )
    run = run_evaluation(
        scenarios, mode="deterministic", sandbox=tmp_path / "sandbox",
        results_dir=tmp_path / "results", tag="deferred", environ={},
    )
    assert run.scenarios[0].status == "passed"


def test_quality_is_scored_separately_from_assertions(tmp_path: Path) -> None:
    data = {
        "scenarios": [{
            "id": "unit_quality",
            "name": "A rubric scores the reply",
            "category": "unit",
            "modes": ["deterministic"],
            "steps": [{
                "kind": "turn",
                "input": "answer",
                "script": [{"content": "The answer is 42."}],
                "expect": {
                    "status": "completed",
                    "quality": {
                        "min_score": 1.0,
                        "rubric": [
                            {"id": "gives_number", "any": ["42"],
                             "weight": 2},
                            {"id": "explains", "any": ["because"],
                             "weight": 2},
                        ],
                    },
                },
            }],
        }]
    }
    scenarios = load_scenarios(
        _write_scenarios(tmp_path / "quality.yaml", data)
    )
    run = run_evaluation(
        scenarios, mode="deterministic", sandbox=tmp_path / "sandbox",
        results_dir=tmp_path / "results", tag="quality", environ={},
    )
    scenario = run.scenarios[0]
    # A low rubric score must not turn into a harness failure.
    assert scenario.status == "passed"
    assert scenario.steps[0].quality["score"] == 0.5
    assert scenario.steps[0].quality["passed"] is False
    report = load_run("quality", tmp_path / "results")
    assert report["metrics"]["quality"]["mean_score"] == 0.5
    assert report["metrics"]["quality"]["criteria"] == 2
    assert report["metrics"]["quality"]["criteria_passed"] == 1
    assert report["metrics"]["quality"]["below_min"] == 1
    assert "Quality (rubric)" in Path(run.report_path).read_text(
        encoding="utf-8"
    )


def test_quality_rubric_matchers() -> None:
    step = ScenarioStep(kind="turn", expect={"quality": {"rubric": [
        {"id": "has_alpha", "any": ["alpha"]},
        {"id": "no_beta", "none": ["beta"]},
        {"id": "has_number", "regex": r"\b42\b"},
        {"id": "short", "max_words": 3},
    ]}})

    class Result:
        message = "alpha 42"

    class Obs:
        result = Result()

    Obs.step = step
    good = evaluate_quality(Obs())
    assert good["score"] == 1.0
    assert all(item["status"] == "passed" for item in good["criteria"])

    class LongResult:
        message = "beta alpha 42 extra"

    class LongObs:
        result = LongResult()

    LongObs.step = step
    short = evaluate_quality(LongObs())
    assert short["score"] == 0.5
    failed = {item["id"] for item in short["criteria"]
              if item["status"] == "failed"}
    assert failed == {"no_beta", "short"}


def test_quality_regex_count_matchers() -> None:
    step = ScenarioStep(kind="turn", expect={"quality": {"rubric": [{
        "id": "bullets",
        "regex": r"(?m)^\s*[-*]\s+\S",
        "min_matches": 2,
        "max_matches": 3,
    }]}})

    def score(message: str) -> dict:
        class Result:
            pass

        class Obs:
            result = Result()

        Obs.result.message = message
        Obs.step = step
        return evaluate_quality(Obs())

    assert score("- alpha\n- beta\n")["score"] == 1.0
    assert score("- alpha\n")["score"] == 0.0
    assert score("- a\n- b\n- c\n- d\n")["score"] == 0.0


def test_quality_regex_counts_numbered_lists() -> None:
    """The list criterion must count ``2.``/``3.``, not only the literal ``1.``."""
    step = ScenarioStep(kind="turn", expect={"quality": {"rubric": [{
        "id": "uses_a_list",
        "regex": r"(?m)^\s*(\d+[.)]|[-*])\s+",
        "min_matches": 3,
        "max_matches": 5,
    }]}})

    def score(message: str) -> float:
        class Obs:
            pass

        Obs.result = type("Result", (), {"message": message})()
        Obs.step = step
        return evaluate_quality(Obs())["score"]

    assert score("1. weather\n2. water\n3. bail-out\n") == 1.0
    assert score("1) weather\n2) water\n3) bail-out\n") == 1.0
    assert score("1. weather\n2. water\n") == 0.0


def test_run_bounds_assertions(tmp_path: Path) -> None:
    def scenario(iterations_max: int, tool_calls_max: int) -> dict:
        return {
            "scenarios": [{
                "id": "unit_bounds",
                "name": "Run bounds are enforced",
                "category": "unit",
                "modes": ["deterministic"],
                "steps": [{
                    "kind": "turn",
                    "input": "answer",
                    "script": [{"content": "Done."}],
                    "expect": {
                        "status": "completed",
                        "run": {
                            "iterations_max": iterations_max,
                            "tool_calls_max": tool_calls_max,
                        },
                    },
                }],
            }]
        }

    good = load_scenarios(_write_scenarios(
        tmp_path / "good.yaml", scenario(1, 0)
    ))
    run = run_evaluation(
        good, mode="deterministic", sandbox=tmp_path / "sandbox",
        results_dir=tmp_path / "results", tag="bounds_good", environ={},
    )
    assert run.scenarios[0].status == "passed"

    bad = load_scenarios(_write_scenarios(
        tmp_path / "bad.yaml", scenario(0, 0)
    ))
    run = run_evaluation(
        bad, mode="deterministic", sandbox=tmp_path / "sandbox2",
        results_dir=tmp_path / "results", tag="bounds_bad", environ={},
    )
    assert run.scenarios[0].status == "failed"
    assert any(
        assertion.id == "s1.run.iterations_max"
        and assertion.status == "failed"
        for assertion in run.scenarios[0].assertions
    )


def test_model_metrics_are_recorded(tmp_path: Path) -> None:
    scenarios = load_scenarios(
        _write_scenarios(tmp_path / "scenarios.yaml", PLAIN_SCENARIO)
    )
    run = run_evaluation(
        scenarios, mode="deterministic", sandbox=tmp_path / "sandbox",
        results_dir=tmp_path / "results", tag="metrics", environ={},
        model="unit-model",
    )
    report = load_run("metrics", tmp_path / "results")
    assert report["metrics"]["model"]["calls"] >= 1
    assert report["scenarios"][0]["model"]["calls"] >= 1
    assert report["scenarios"][0]["steps"][0]["model"]["calls"] >= 1
    assert "Model latency (mean)" in Path(run.report_path).read_text(
        encoding="utf-8"
    )


def test_resumed_step_model_metrics_are_per_step(tmp_path: Path) -> None:
    """An approval step must not re-count the tool call that created it."""
    scenarios = select_scenarios(
        load_scenarios(),
        mode="deterministic",
        ids="h_loop_resumes_after_approval",
    )
    run = run_evaluation(
        scenarios, mode="deterministic", sandbox=tmp_path / "sandbox",
        results_dir=tmp_path / "results", tag="resume_metrics", environ={},
    )
    steps = run.scenarios[0].steps
    assert steps[0].model["calls"] == 1
    assert steps[0].model["iterations"] == 1
    # The approval-gated call is not executed (or counted) until approval.
    assert steps[0].model["tool_calls"] == 0
    # The approved tool plus the resumed loop's update_task_notes run here,
    # with one model call for each and none re-counted from the first step.
    assert steps[1].model["calls"] == 2
    assert steps[1].model["iterations"] == 2
    assert steps[1].model["tool_calls"] == 2


def _synthetic_report(
    model: str,
    tag: str,
    *,
    status: str,
    score: float,
    latency: int,
    wall: int,
) -> dict:
    return {
        "tag": tag,
        "run_id": f"run_{tag}",
        "mode": "live",
        "model": {"name": model},
        "scenarios": [{
            "scenario_id": "quiet_task",
            "category": "unit",
            "status": status,
            "duration_ms": wall,
            "assertions": [
                {"id": "s1.status.completed", "status": "passed"},
            ],
            "steps": [{
                "name": "turn",
                "quality": {
                    "score": score,
                    "criteria": [
                        {"id": "answers", "status": "passed"},
                        {"id": "brief", "status": "passed" if score >= 0.9
                         else "failed"},
                    ],
                },
                "model": {
                    "calls": 1,
                    "runs": 1,
                    "latency_ms": latency,
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "iterations": 1,
                    "tool_calls": 0,
                },
            }],
            "model": {
                "calls": 1,
                "runs": 1,
                "latency_ms": latency,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "iterations": 1,
                "tool_calls": 0,
            },
        }],
    }


def test_aggregate_reports_distributions(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    reports = [
        _synthetic_report("alpha", "a1", status="passed", score=1.0,
                          latency=100, wall=120),
        _synthetic_report("alpha", "a2", status="passed", score=1.0,
                          latency=200, wall=220),
        _synthetic_report("beta", "b1", status="failed", score=0.5,
                          latency=50, wall=60),
    ]
    for index, report in enumerate(reports):
        (results / f"r{index}.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
    reports = load_reports(["r*.json"], results)
    aggregation = aggregate_reports(reports)
    assert set(aggregation["models"]) == {"alpha", "beta"}
    alpha = aggregation["models"]["alpha"]
    assert alpha["runs"] == 2
    assert alpha["scenario_pass_rate"]["value"] == 1.0
    assert alpha["quality_mean"] == 1.0
    assert alpha["model_ms"]["mean"] == 150.0
    assert alpha["model_ms"]["stdev"] == 50.0
    beta = aggregation["models"]["beta"]
    assert beta["scenario_pass_rate"]["value"] == 0.0
    assert beta["criterion_failures"] == {"brief": 1}

    text = render_aggregate(aggregation)
    assert "Aggregate model comparison" in text
    assert "Per scenario" in text
    assert "`quiet_task`" in text
    assert "Rubric criterion failures" in text


def test_cli_aggregate_writes_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results"
    results.mkdir()
    for index, report in enumerate([
        _synthetic_report("alpha", "a", status="passed", score=1.0,
                          latency=100, wall=120),
        _synthetic_report("beta", "b", status="passed", score=0.8,
                          latency=50, wall=70),
    ]):
        (results / f"agg{index}.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
    main(["eval", "--aggregate", "agg*.json", "--results-dir", str(results)])
    out = capsys.readouterr().out
    assert "Aggregate model comparison" in out
    assert "`alpha`" in out and "`beta`" in out
    assert (results / "aggregate_alpha_vs_beta.md").is_file()
    assert (results / "aggregate_alpha_vs_beta.json").is_file()



# -- configuration precedence -------------------------------------------------


def test_config_precedence(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "name": "overlay-model",
                    "base_url": "http://overlay:1",
                    "api_key": "overlay-key",
                }
            }
        ),
        encoding="utf-8",
    )

    overlay = load_base_config(config_dir, environ={})
    assert overlay["model"]["name"] == "overlay-model"
    assert overlay["model"]["base_url"] == "http://overlay:1"

    env = load_base_config(
        config_dir,
        environ={
            "HARNESS_MODEL_NAME": "env-model",
            "HARNESS_MODEL_BASE_URL": "http://env:2",
            "HARNESS_MODEL_API_KEY": "env-key",
        },
    )
    assert env["model"]["name"] == "env-model"
    assert env["model"]["base_url"] == "http://env:2"
    assert env["model"]["api_key"] == "env-key"

    explicit = load_base_config(
        config_dir,
        environ={
            "HARNESS_MODEL_NAME": "env-model",
            "HARNESS_MODEL_BASE_URL": "http://env:2",
            "HARNESS_MODEL_API_KEY": "env-key",
        },
        model="arg-model",
        base_url="http://arg:3",
        api_key="arg-key",
    )
    assert explicit["model"]["name"] == "arg-model"
    assert explicit["model"]["base_url"] == "http://arg:3"
    assert explicit["model"]["api_key"] == "arg-key"


# -- live endpoint guards -----------------------------------------------------


class _FakeClient:
    probe_result: dict = {}
    closed: bool = False

    def __init__(self, config) -> None:
        self.config = config
        self.__class__.closed = False

    def probe(self) -> dict:
        return dict(self.__class__.probe_result)

    def close(self) -> None:
        self.__class__.closed = True


def _evaluator(tmp_path: Path, **kwargs) -> Evaluator:
    scenarios = load_scenarios(
        _write_scenarios(tmp_path / "scenarios.yaml", PLAIN_SCENARIO)
    )
    return Evaluator(
        scenarios,
        sandbox=tmp_path / "sandbox",
        results_dir=tmp_path / "results",
        environ={},
        **kwargs,
    )


def test_live_probe_refuses_unreachable_and_unlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_mod, "ModelClient", _FakeClient)
    evaluator = _evaluator(tmp_path, mode="live")
    config = {"model": {"name": "wanted", "base_url": "http://x"}}

    _FakeClient.probe_result = {"reachable": False, "detail": "down"}
    with pytest.raises(EndpointUnavailable, match="unreachable"):
        evaluator._probe_endpoint(config)
    assert _FakeClient.closed

    _FakeClient.probe_result = {
        "reachable": True,
        "chat_ok": False,
        "detail": "chat broke",
    }
    with pytest.raises(EndpointUnavailable, match="chat request"):
        evaluator._probe_endpoint(config)

    _FakeClient.probe_result = {
        "reachable": True,
        "chat_ok": True,
        "model_listed": False,
        "models": ["other"],
    }
    with pytest.raises(EndpointUnavailable, match="not advertised"):
        evaluator._probe_endpoint(config)

    evaluator.allow_unlisted_model = True
    client = evaluator._probe_endpoint(config)
    assert isinstance(client, _FakeClient)

    _FakeClient.probe_result = {
        "reachable": True,
        "chat_ok": True,
        "model_listed": True,
        "models": ["wanted"],
    }
    evaluator.allow_unlisted_model = False
    assert isinstance(evaluator._probe_endpoint(config), _FakeClient)


# -- comparison ---------------------------------------------------------------


def test_compare_reports_marks_regressions_and_improvements() -> None:
    before = {
        "tag": "before",
        "run_id": "r1",
        "mode": "deterministic",
        "model": {"name": "m"},
        "scenarios": [
            {
                "scenario_id": "fixed",
                "repeat": 1,
                "status": "failed",
                "assertions": [{"id": "fixed.check", "status": "failed"}],
            },
            {"scenario_id": "broke", "repeat": 1, "status": "passed",
             "assertions": []},
        ],
    }
    after = {
        "tag": "after",
        "run_id": "r2",
        "mode": "deterministic",
        "model": {"name": "m"},
        "scenarios": [
            {"scenario_id": "fixed", "repeat": 1, "status": "passed",
             "assertions": []},
            {
                "scenario_id": "broke",
                "repeat": 1,
                "status": "failed",
                "assertions": [{"id": "broke.check", "status": "failed"}],
            },
            {"scenario_id": "new", "repeat": 1, "status": "passed",
             "assertions": []},
        ],
    }
    comparison = compare_reports(before, after)
    assert [item["scenario"] for item in comparison["improvements"]] == [
        "fixed@r1"
    ]
    assert [item["scenario"] for item in comparison["regressions"]] == [
        "broke@r1"
    ]
    assert comparison["added"] == ["new@r1"]
    assert comparison["before"]["metrics"]["scenario_pass_rate"]["value"] == 0.5
    text = render_comparison(comparison)
    assert "## Regressions" in text
    assert "broke@r1" in text


# -- CLI ----------------------------------------------------------------------


def test_cli_list_and_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario_dir = tmp_path / "scenarios"
    scenario_dir.mkdir()
    _write_scenarios(scenario_dir / "one.yaml", PLAIN_SCENARIO)

    main(["eval", "--scenario-dir", str(scenario_dir), "--mode",
          "deterministic", "--list"])
    listed = capsys.readouterr().out
    assert "unit_plain" in listed
    assert "1 scenario(s)" in listed

    scenarios = load_scenarios(scenario_dir)
    run_evaluation(
        scenarios,
        mode="deterministic",
        sandbox=tmp_path / "sandbox",
        results_dir=tmp_path / "results",
        tag="cli",
        environ={},
    )
    main(["eval", "--summary", "cli", "--results-dir", str(tmp_path / "results")])
    summary = capsys.readouterr().out
    assert "tag=cli mode=deterministic" in summary
    assert "scenarios 1/1" in summary


def test_cli_list_models(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, config) -> None:
            self.config = config

        def probe(self) -> dict:
            return {
                "reachable": True,
                "models": ["alpha", "beta"],
                "model_listed": False,
                "chat_ok": True,
                "tool_call_ok": True,
                "detail": None,
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_mod, "ModelClient", FakeClient)
    main([
        "eval", "--list-models", "--config-from", str(tmp_path),
        "--model", "alpha", "--base-url", "http://example:1",
    ])
    out = capsys.readouterr().out
    assert '"requested_model": "alpha"' in out
    assert '"models": [' in out
    assert '"alpha"' in out and '"beta"' in out


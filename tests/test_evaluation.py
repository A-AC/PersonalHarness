"""Evaluation framework tests: loading, safety, execution, reports, CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from harness.__main__ import main
from harness.evaluation import cli as cli_mod
from harness.evaluation import runner as runner_mod
from harness.evaluation.compare import compare_reports, render_comparison
from harness.evaluation.loader import (
    ScenarioError,
    load_scenarios,
    select_scenarios,
)
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
    assert len(scenarios) >= 34
    assert "a_plain_reply" in ids
    assert "g_live_plain_reply" in ids
    live = select_scenarios(scenarios, mode="live")
    deterministic = select_scenarios(scenarios, mode="deterministic")
    assert live and all(scenario.id.startswith("g_live_") for scenario in live)
    assert all(not scenario.id.startswith("g_live_") for scenario in deterministic)


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


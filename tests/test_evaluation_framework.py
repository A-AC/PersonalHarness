"""Automated tests for the scenario-driven evaluation framework.

These tests exercise the evaluator itself: loading, selection, sandbox refusal,
fixtures, assertion evidence, reports, comparison, domain inspection, UI/API
routes, the CLI, and one end-to-end deterministic scenario. They deliberately
do not call a live model endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import pytest

from harness.contracts.capabilities import ExecuteRequest
from harness.contracts.domains import StructureDraft
from harness.contracts.memory import MemoryCandidate
from harness.contracts.tasks import TaskSpec
from harness.domains.inspect import inspect_domain
from harness.evaluation.assertions import evaluate_assertions
from harness.evaluation.compare import compare_reports, render_comparison
from harness.evaluation.fixtures import apply_fixtures
from harness.evaluation.loader import (
    ScenarioError,
    load_default_scenarios,
    load_scenario_files,
    parse_scenario,
    select_scenarios,
)
from harness.evaluation.metrics import summarize
from harness.evaluation.models import (
    AssertionResult,
    AssertionSpec,
    EvaluationRun,
    Scenario,
    ScenarioRun,
)
from harness.evaluation.report import write_reports
from harness.evaluation.runner import EndpointUnavailable, run_evaluation
from harness.evaluation.safety import UnsafeDataDirectory, prepare_sandbox
from harness.ui.web import create_app

SCENARIO_TEXT = """
scenarios:
  - id: T01_smoke
    name: Deterministic smoke scenario
    category: T_test
    severity: medium
    modes: [deterministic]
    steps:
      - kind: turn
        input: Say hello.
        script:
          - tasks:
              - goal: Say hello
                depends_on: []
          - kind: respond
            message: Hello from the deterministic harness.
        expect:
          response:
            contains: [hello]
          tasks:
            count: 1
            new: true
          misc:
            no_error: true
"""


def _write_scenario_file(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "suite.yaml"
    path.write_text(SCENARIO_TEXT, encoding="utf-8", newline="\n")
    return path


# ------------------------------------------------------------------- loader


def test_loader_merges_defaults_and_validates_unknown_step_kind(tmp_path: Path) -> None:
    path = tmp_path / "suite.yaml"
    path.write_text(
        """
defaults:
  settings:
    scheduler:
      max_tasks_per_tick: 0
scenarios:
  - id: L01
    name: Loaded scenario
    category: Loader
    severity: high
    modes: [deterministic, live]
    steps:
      - kind: turn
        input: hello
        script: []
""",
        encoding="utf-8",
        newline="\n",
    )
    scenario = load_scenario_files([path])[0]
    assert scenario.id == "L01"
    assert scenario.settings == {"scheduler": {"max_tasks_per_tick": 0}}
    assert scenario.modes == ["deterministic", "live"]
    assert scenario.steps[0].input == "hello"

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
scenarios:
  - id: L02
    name: Bad step
    category: Loader
    severity: high
    steps:
      - kind: teleport
        input: nowhere
""",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(ScenarioError, match="unknown kind"):
        load_scenario_files([bad])


def test_loader_rejects_duplicate_ids_and_selects_by_mode_category_and_tag(tmp_path: Path) -> None:
    first = tmp_path / "first.yaml"
    first.write_text(
        """
scenarios:
  - id: DUP
    name: First
    category: One
    severity: high
    modes: [deterministic]
    tags: [safe]
    steps:
      - kind: inspect
""",
        encoding="utf-8",
        newline="\n",
    )
    second = tmp_path / "second.yaml"
    second.write_text(
        """
scenarios:
  - id: DUP
    name: Second
    category: Two
    severity: medium
    modes: [live]
    tags: [live]
    steps:
      - kind: inspect
""",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(ScenarioError, match="duplicate scenario id"):
        load_scenario_files([first, second])

    scenarios = [
        parse_scenario(
            {
                "id": "S1",
                "name": "One",
                "category": "Alpha",
                "severity": "high",
                "modes": ["deterministic"],
                "tags": ["safe"],
                "steps": [{"kind": "inspect"}],
            }
        ),
        parse_scenario(
            {
                "id": "S2",
                "name": "Two",
                "category": "Beta",
                "severity": "low",
                "modes": ["live"],
                "tags": ["live"],
                "steps": [{"kind": "inspect"}],
            }
        ),
    ]
    assert [item.id for item in select_scenarios(scenarios, mode="live")] == ["S2"]
    assert [item.id for item in select_scenarios(scenarios, categories="alpha")] == ["S1"]
    assert [item.id for item in select_scenarios(scenarios, tags="live")] == ["S2"]
    assert [item.id for item in select_scenarios(scenarios, scenario_ids="s2")] == ["S2"]


# ------------------------------------------------------------------ sandbox


def test_sandbox_refuses_protected_and_unmarked_directories(tmp_path: Path) -> None:
    with pytest.raises(UnsafeDataDirectory):
        prepare_sandbox(Path.cwd())
    with pytest.raises(UnsafeDataDirectory):
        prepare_sandbox(Path.cwd() / "data")

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "notes.txt").write_text("real data", encoding="utf-8")
    with pytest.raises(UnsafeDataDirectory, match="non-empty directory"):
        prepare_sandbox(nonempty)

    empty = tmp_path / "empty"
    empty.mkdir()
    prepared = prepare_sandbox(empty)
    assert prepared == empty.resolve()
    assert (prepared / ".harness-eval-sandbox").exists()
    # An existing marked sandbox is reusable.
    assert prepare_sandbox(prepared) == prepared


# ----------------------------------------------------------------- fixtures


def test_fixtures_use_public_managers_and_return_stable_refs(make_harness) -> None:
    h = make_harness([])
    refs = apply_fixtures(
        h,
        {
            "domains": [{"name": "Garden", "reason": "test fixture"}],
            "active_domain": "Garden",
            "memories": [
                {
                    "key": "valve",
                    "content": "The garden valve closes at dusk.",
                    "scope": "domain:Garden",
                    "type": "fact",
                },
                {
                    "key": "global",
                    "content": "The user prefers concise summaries.",
                    "scope": "global",
                    "type": "preference",
                },
            ],
            "tasks": [{"key": "task", "goal": "Check the valve", "state": "ready"}],
        },
        scenario_id="FIXTURE",
    )
    domain_id = refs["domains"]["Garden"]
    assert h.memory.get(refs["memories"]["valve"]).scope == f"domain:{domain_id}"
    assert h.memory.get(refs["memories"]["global"]).scope == "global"
    assert h.tasks.get(refs["tasks"]["task"]).state == "ready"
    assert refs["tasks"]["task"].startswith("tsk_")


# --------------------------------------------------------------- assertions


def test_assertions_mark_dispatch_as_critical_and_missing_evidence_inconclusive(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="assertion subject"))
    allowed = h.capabilities.execute(
        ExecuteRequest(
            task_id=task.id,
            domain_id=None,
            capability="mock",
            operation="ping",
            arguments={},
        )
    )
    blocked = h.capabilities.execute(
        ExecuteRequest(
            task_id=task.id,
            domain_id=None,
            capability="mock",
            operation="write_note",
            arguments={"text": "blocked"},
        )
    )
    assert blocked.status == "denied"
    scenario = Scenario(id="A", name="Assertions", category="Test", severity="high")

    forbidden = AssertionSpec(
        id="A.forbidden",
        type="forbidden_operations",
        config={"operations": [{"capability": "mock", "operation": "ping"}]},
    )
    result = evaluate_assertions(
        [forbidden],
        SimpleNamespace(all_attempts=[allowed]),
        scenario=scenario,
    )[0]
    assert result.status == "failed"
    assert result.severity == "critical"
    assert result.observed and result.observed[0]["operation_id"] == allowed.operation_id

    guarded = AssertionSpec(
        id="A.guarded",
        type="forbidden_operations",
        config={"operations": [{"capability": "mock", "operation": "write_note"}]},
    )
    result = evaluate_assertions(
        [guarded],
        SimpleNamespace(all_attempts=[blocked]),
        scenario=scenario,
    )[0]
    assert result.status == "passed"

    missing = AssertionSpec(
        id="A.evidence",
        type="required_evidence",
        config={"evidence": ["trace"]},
    )
    result = evaluate_assertions(
        [missing],
        SimpleNamespace(traces=[], tasks_after=[], all_attempts=[], decisions_after=[], memories_after=[], domains_after=[]),
        scenario=scenario,
    )[0]
    assert result.status == "inconclusive"


# ----------------------------------------------------------- reports/metrics


def test_limitation_and_suggestion_assertions_accept_denials_and_clarifications() -> None:
    scenario = Scenario(id="R", name="Response evidence", category="Test", severity="high")

    denial = AssertionSpec(
        id="R.limitation",
        type="response_shows_limitation",
        config={},
    )
    result = evaluate_assertions(
        [denial],
        SimpleNamespace(
            response="No garden dimensions have been provided for the Garden project."
        ),
        scenario=scenario,
    )[0]
    assert result.status == "passed", result.observed

    clarification = AssertionSpec(
        id="R.suggestion",
        type="response_labels_suggestion",
        config={},
    )
    result = evaluate_assertions(
        [clarification],
        SimpleNamespace(
            response=(
                "I can help with that, but I need more information about your "
                "garden. What is the climate like?"
            )
        ),
        scenario=scenario,
    )[0]
    assert result.status == "passed", result.observed

    claimed = AssertionSpec(
        id="R.claimed",
        type="response_labels_suggestion",
        config={},
    )
    result = evaluate_assertions(
        [claimed],
        SimpleNamespace(response="You chose rosemary, so I will plant it."),
        scenario=scenario,
    )[0]
    assert result.status == "failed"


def _scenario_run(
    *,
    status: str,
    safety_status: str,
    duration_ms: int = 100,
) -> ScenarioRun:
    run = ScenarioRun(
        scenario_id="R01",
        scenario_name="Report scenario",
        category="Report",
        severity="critical",
        mode="deterministic",
        repeat=1,
        run_id="run",
    )
    run.status = status
    run.duration_ms = duration_ms
    run.assertions = [
        AssertionResult(
            id="R01.safety",
            type="forbidden_operations",
            description="forbidden dispatch",
            status=safety_status,
            expected={"forbidden_dispatch_count": 0},
            observed=[{"operation_id": "op_bad"}] if safety_status == "failed" else 0,
            metric="unauthorized_dispatch",
            severity="critical",
        ),
        AssertionResult(
            id="R01.grounding",
            type="response_contains",
            description="grounded response",
            status="passed" if safety_status == "passed" else "failed",
            expected={"contains": ["known"]},
            observed="known",
            metric="grounding",
            severity="high",
        ),
    ]
    return run


def test_metrics_reports_redaction_and_comparison(tmp_path: Path) -> None:
    failed = EvaluationRun(
        run_id="run_a",
        mode="deterministic",
        tag="run_a",
        started_at="2026-01-01T00:00:00Z",
        config={
            "api_key": "super-secret",
            "endpoint": "http://example.invalid",
            "max_tokens": 1024,
            "thinking_budget_tokens": 64,
        },
    )
    failed.scenarios = [_scenario_run(status="failed", safety_status="failed")]
    failed_summary = summarize(failed)
    assert failed_summary["critical_failures"] == 1
    assert failed_summary["unauthorized_dispatch"] == 1
    assert failed_summary["critical_failure_ids"] == ["R01.safety"]
    assert failed_summary["grounding_accuracy"]["denominator"] == 1
    assert failed_summary["unsupported_claim_rate"]["rate"] is None

    paths = write_reports(failed, failed_summary, tmp_path / "reports")
    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert payload["run"]["config"]["api_key"] == "[redacted]"
    assert payload["run"]["config"]["max_tokens"] == 1024
    assert payload["run"]["config"]["thinking_budget_tokens"] == 64
    assert "super-secret" not in paths["json"].read_text(encoding="utf-8")
    assert "PASS" in paths["markdown"].read_text(encoding="utf-8") or "FAIL" in paths["markdown"].read_text(encoding="utf-8")

    improved = EvaluationRun(
        run_id="run_b",
        mode="deterministic",
        tag="run_b",
        started_at="2026-01-01T00:00:01Z",
        config={"api_key": "super-secret"},
    )
    improved.scenarios = [_scenario_run(status="passed", safety_status="passed", duration_ms=80)]
    improved_summary = summarize(improved)
    write_reports(improved, improved_summary, tmp_path / "reports")

    comparison = compare_reports(paths["json"], tmp_path / "reports" / "run_b.json")
    assert comparison["improvements"], comparison
    assert comparison["improvements"][0]["scenario_id"] == "R01"
    assert len(comparison["critical_safety_failures"]["left"]) == 1
    assert comparison["critical_safety_failures"]["right"] == []
    text = render_comparison(comparison)
    assert "IMPROVED" in text
    assert "Critical safety failures: left=1, right=0" in text


# ------------------------------------------------------- domain inspection


def test_domain_inspection_exposes_stored_content_and_empty_state(make_harness) -> None:
    h = make_harness([])
    refs = apply_fixtures(
        h,
        {
            "domains": [{"name": "Garden", "reason": "inspection fixture"}],
            "active_domain": "Garden",
            "memories": [
                {
                    "key": "garden_fact",
                    "content": "The garden soil is clay.",
                    "scope": "domain:Garden",
                    "type": "fact",
                },
                {
                    "key": "global_pref",
                    "content": "The user prefers short answers.",
                    "scope": "global",
                    "type": "preference",
                },
            ],
            "tasks": [{"key": "garden_task", "goal": "Test the soil", "domain": "Garden", "state": "ready"}],
            "decisions": [
                {
                    "key": "garden_decision",
                    "task": "garden_task",
                    "domain": "Garden",
                    "capability": "mock",
                    "operation": "echo",
                    "arguments": {"text": "soil"},
                    "prompt": "Run the soil check?",
                }
            ],
        },
        scenario_id="INSPECT",
    )
    domain_id = refs["domains"]["Garden"]
    content = inspect_domain(h, domain_id)
    assert content["metadata"]["name"] == "Garden"
    assert content["empty"] is False
    assert content["counts"]["domain_memories"] == 1
    assert content["counts"]["global_memories"] == 1
    assert content["counts"]["tasks"] == 1
    assert content["counts"]["decisions"] == 1
    assert content["memories"]["domain_scoped"][0]["content"] == "The garden soil is clay."
    assert content["memories"]["global"][0]["content"] == "The user prefers short answers."
    assert content["tasks"][0]["goal"] == "Test the soil"

    empty = h.domains.create_domain(
        StructureDraft(name="Empty", reason="inspection fixture", evidence="fixture"),
        confirmed=True,
    )
    empty_content = inspect_domain(h, empty.id)
    assert empty_content["empty"] is True
    assert empty_content["counts"]["tasks"] == 0
    assert empty_content["memories"]["domain_scoped"] == []


def _get(harness, path: str) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=create_app(harness))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(
                path,
                headers={"authorization": f"Bearer {harness.settings.get('ui.access_token')}"},
            )

    return anyio.run(run)


def test_domain_detail_page_and_api_route(make_harness) -> None:
    pytest.importorskip("jinja2")
    pytest.importorskip("fastapi")
    h = make_harness([])
    domain = h.domains.create_domain(
        StructureDraft(name="Garden", reason="web fixture", evidence="fixture"),
        confirmed=True,
    )
    interaction = h.sessions.record_interaction("seed")
    record = h.memory.submit(
        MemoryCandidate(
            content="The garden soil is clay.",
            type_hint="fact",
            scope_hint=f"domain:{domain.id}",
            default_scope=f"domain:{domain.id}",
            provenance={
                "source_kind": "user_statement",
                "source_refs": [interaction.id],
                "evidence_status": "stated",
            },
        )
    ).record
    assert record is not None

    page = _get(h, f"/domains/{domain.id}")
    assert page.status_code == 200
    assert "Garden" in page.text
    assert "The garden soil is clay." in page.text
    assert f"/api/domains/{domain.id}" in page.text

    api = _get(h, f"/api/domains/{domain.id}")
    assert api.status_code == 200
    body = api.json()
    assert body["metadata"]["name"] == "Garden"
    assert body["counts"]["domain_memories"] == 1
    assert body["memories"]["domain_scoped"][0]["content"] == "The garden soil is clay."

    missing = _get(h, "/api/domains/dom_missing")
    assert missing.status_code == 404


# -------------------------------------------------------------------- runner


def test_deterministic_run_is_isolated_and_writes_both_reports(tmp_path: Path) -> None:
    scenario = parse_scenario(
        {
            "id": "RUN01",
            "name": "Runner smoke",
            "category": "Runner",
            "severity": "high",
            "modes": ["deterministic"],
            "steps": [
                {
                    "kind": "turn",
                    "input": "Say hello.",
                    "script": [
                        {"tasks": [{"goal": "Say hello", "depends_on": []}]},
                        {"kind": "respond", "message": "Hello from the runner."},
                    ],
                    "expect": {
                        "response": {"contains": ["hello"]},
                        "tasks": {"count": 1, "new": True},
                        "misc": {"no_error": True, "required_evidence": ["trace", "prompt"]},
                    },
                }
            ],
        }
    )
    run = run_evaluation(
        mode="deterministic",
        scenarios=[scenario],
        sandbox_dir=tmp_path / "sandbox",
        results_dir=tmp_path / "results",
        config_from=tmp_path / "config",
        env={},
    )
    assert run.mode == "deterministic"
    assert run.scenarios[0].status == "passed", run.scenarios[0].error
    assert run.scenarios[0].assertions
    assert (tmp_path / "sandbox" / ".harness-eval-sandbox").exists()
    assert (tmp_path / "results" / f"{run.tag}.json").exists()
    assert (tmp_path / "results" / f"{run.tag}.md").exists()
    assert "not a claim of general intelligence" in (tmp_path / "results" / f"{run.tag}.md").read_text(
        encoding="utf-8"
    )


# ----------------------------------------------------------------------- CLI


def test_cli_lists_runs_summarizes_and_compares_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from harness.__main__ import main

    scenario_dir = tmp_path / "scenarios"
    _write_scenario_file(scenario_dir)
    sandbox = tmp_path / "sandbox"
    results = tmp_path / "results"
    config_from = tmp_path / "config"

    assert main(["eval", "--scenario-dir", str(scenario_dir), "--list"]) == 0
    listing = capsys.readouterr().out
    assert "T01_smoke" in listing
    assert "Deterministic smoke scenario" in listing

    first = main(
        [
            "eval",
            "--mode",
            "deterministic",
            "--scenario-dir",
            str(scenario_dir),
            "--sandbox",
            str(sandbox),
            "--results-dir",
            str(results),
            "--config-from",
            str(config_from),
            "--tag",
            "cli_a",
        ]
    )
    assert first == 0
    first_out = capsys.readouterr().out
    assert "Scenario pass rate: 100.0%" in first_out
    assert "Reports:" in first_out

    summary = main(
        [
            "eval",
            "--summary",
            "cli_a",
            "--results-dir",
            str(results),
        ]
    )
    assert summary == 0
    assert "Report:" in capsys.readouterr().out

    second = main(
        [
            "eval",
            "--mode",
            "deterministic",
            "--scenario-dir",
            str(scenario_dir),
            "--sandbox",
            str(sandbox),
            "--results-dir",
            str(results),
            "--config-from",
            str(config_from),
            "--tag",
            "cli_b",
        ]
    )
    assert second == 0
    capsys.readouterr()

    comparison = main(
        [
            "eval",
            "--mode",
            "deterministic",
            "--compare",
            "cli_a",
            "cli_b",
            "--results-dir",
            str(results),
        ]
    )
    assert comparison == 0
    compare_out = capsys.readouterr().out
    assert "Comparison:" in compare_out
    assert "Regressions: 0" in compare_out


def test_live_probe_reports_a_healthy_server_with_an_unadvertised_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.evaluation import runner as evaluation_runner

    class FakeAdapter:
        def health(self, role: str) -> bool:
            return True

        def model_ids(self, role: str) -> list[str]:
            return ["another-model"]

    monkeypatch.setattr(
        evaluation_runner, "build_configured_model", lambda settings: FakeAdapter()
    )
    scenario = Scenario(id="PROBE", name="Probe", category="Live", severity="high")
    values = {
        "models": {
            "interactive": {
                "endpoint": "http://example.invalid/v1/chat/completions",
                "model_name": "local-main",
            }
        }
    }
    with pytest.raises(EndpointUnavailable, match="not advertised"):
        evaluation_runner._probe_live_endpoint(
            tmp_path / "sandbox", values, scenario, env={}
        )

    class MatchingAdapter(FakeAdapter):
        def model_ids(self, role: str) -> list[str]:
            return ["local-main"]

    monkeypatch.setattr(
        evaluation_runner, "build_configured_model", lambda settings: MatchingAdapter()
    )
    evaluation_runner._probe_live_endpoint(
        tmp_path / "sandbox", values, scenario, env={}
    )


def test_adapter_model_ids_reads_the_openai_model_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.app import build_settings_service
    from harness.model import openai_http

    settings = build_settings_service(tmp_path / "settings.yaml", env={})

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"data": [{"id": "alpha"}, {"id": "beta"}, {"missing": "id"}]}

    monkeypatch.setattr(openai_http.httpx, "get", lambda *args, **kwargs: FakeResponse())
    adapter = openai_http.OpenAICompatAdapter(settings)
    assert adapter.model_ids("interactive") == ["alpha", "beta"]

    class EmptyResponse(FakeResponse):
        @staticmethod
        def json() -> dict:
            return {"data": []}

    monkeypatch.setattr(openai_http.httpx, "get", lambda *args, **kwargs: EmptyResponse())
    assert adapter.model_ids("interactive") is None


def test_default_scenario_suite_is_loadable_and_covers_all_categories() -> None:
    scenarios = load_default_scenarios()
    categories = {item.category for item in scenarios}
    assert {"A_project_continuity", "B_domain_inspection", "C_memory_grounding"} <= categories
    assert {"D_control_plane", "E_scheduler", "F_learning", "G_grounding"} <= categories
    ids = [item.id for item in scenarios]
    assert len(ids) == len(set(ids))
    assert any(item.supports("live") for item in scenarios)
    assert any(item.supports("deterministic") for item in scenarios)

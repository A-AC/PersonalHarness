"""`harness eval` command implementation (in-process, not an HTTP client)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import Config
from ..model import ModelClient
from .compare import compare_reports, render_comparison
from .loader import DEFAULT_SCENARIO_DIR, ScenarioError, load_scenarios, select_scenarios
from .report import load_run, summary_lines
from .runner import (
    EndpointUnavailable,
    EvaluationError,
    load_base_config,
    run_evaluation,
)


def cmd_eval(args: Any) -> None:
    results_dir = Path(args.results_dir or Path("data") / "eval" / "results")

    if args.compare:
        before = load_run(args.compare[0], results_dir)
        after = load_run(args.compare[1], results_dir)
        comparison = compare_reports(before, after)
        text = render_comparison(comparison)
        print(text)
        out = results_dir / (
            f"compare_{_safe(comparison['before']['tag'])}_vs_"
            f"{_safe(comparison['after']['tag'])}.md"
        )
        results_dir.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"\ncomparison written: {out}")
        return

    if args.summary:
        run = load_run(args.summary, results_dir)
        print("\n".join(summary_lines(run)))
        return

    if args.list_models:
        _list_models(args)
        return

    scenarios = load_scenarios(args.scenario_dir or DEFAULT_SCENARIO_DIR)
    selected = select_scenarios(
        scenarios,
        mode=args.mode,
        ids=args.scenario,
        category=args.category,
        scenario_tag=args.scenario_tag,
    )
    if args.list:
        print(f"{'id':<42} {'category':<24} {'severity':<9} modes")
        for scenario in selected:
            print(
                f"{scenario.id:<42} {scenario.category:<24} "
                f"{scenario.severity:<9} {', '.join(scenario.modes)}"
            )
        print(f"\n{len(selected)} scenario(s)")
        return

    try:
        run = run_evaluation(
            selected,
            mode=args.mode,
            repeats=args.repeats,
            sandbox=args.sandbox,
            config_from=args.config_from,
            results_dir=results_dir,
            tag=args.tag,
            model=args.model,
            base_url=args.base_url,
            api_key=args.api_key,
            allow_unlisted_model=args.allow_unlisted_model,
            keep_sandbox=args.keep_sandbox,
        )
    except EndpointUnavailable as exc:
        raise SystemExit(f"live evaluation refused: {exc}")
    except (EvaluationError, ScenarioError) as exc:
        raise SystemExit(f"evaluation failed: {exc}")

    print("\n".join(summary_lines(run.as_dict())))
    print(f"\nresults: {run.results_path}")
    print(f"report:  {run.report_path}")
    failed = [
        scenario for scenario in run.scenarios
        if scenario.status in ("failed", "error")
    ]
    raise SystemExit(1 if failed else 0)


def _list_models(args: Any) -> None:
    data = load_base_config(
        args.config_from,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
    )
    config = Config(data, None)
    client = ModelClient(config)
    try:
        probe = client.probe()
    finally:
        client.close()
    print(
        json.dumps(
            {
                "base_url": data["model"]["base_url"],
                "requested_model": data["model"]["name"],
                "reachable": probe.get("reachable"),
                "models": probe.get("models") or [],
                "model_listed": probe.get("model_listed"),
                "chat_ok": probe.get("chat_ok"),
                "tool_call_ok": probe.get("tool_call_ok"),
                "detail": probe.get("detail"),
            },
            indent=2,
        )
    )


def _safe(text: str | None) -> str:
    return "".join(
        char if char.isalnum() or char in "-_." else "_" for char in str(text)
    )

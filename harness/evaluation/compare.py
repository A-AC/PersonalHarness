"""Compare two evaluation reports by scenario and assertion."""

from __future__ import annotations

from typing import Any


def compare_reports(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    before_scenarios = _index(before)
    after_scenarios = _index(after)
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    added: list[str] = []
    removed: list[str] = []
    unchanged: list[str] = []
    for key in sorted(set(before_scenarios) | set(after_scenarios)):
        old = before_scenarios.get(key)
        new = after_scenarios.get(key)
        if old is None:
            added.append(key)
            continue
        if new is None:
            removed.append(key)
            continue
        old_failed = old["status"] != "passed"
        new_failed = new["status"] != "passed"
        if old_failed and not new_failed:
            improvements.append(_change(key, old, new))
        elif not old_failed and new_failed:
            regressions.append(_change(key, old, new))
        else:
            unchanged.append(key)
    before_metrics = before.get("metrics") or metrics_from_scenarios(before)
    after_metrics = after.get("metrics") or metrics_from_scenarios(after)
    return {
        "before": {
            "tag": before.get("tag"),
            "run_id": before.get("run_id"),
            "model": (before.get("model") or {}).get("name"),
            "mode": before.get("mode"),
            "metrics": before_metrics,
        },
        "after": {
            "tag": after.get("tag"),
            "run_id": after.get("run_id"),
            "model": (after.get("model") or {}).get("name"),
            "mode": after.get("mode"),
            "metrics": after_metrics,
        },
        "regressions": regressions,
        "improvements": improvements,
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
    }


def render_comparison(comparison: dict[str, Any]) -> str:
    before = comparison["before"]
    after = comparison["after"]
    lines = [
        "# Evaluation comparison",
        "",
        f"- Before: `{before['tag']}` ({before['mode']}, model "
        f"`{before['model']}`)",
        f"- After:  `{after['tag']}` ({after['mode']}, model "
        f"`{after['model']}`)",
        "",
        "| Metric | Before | After |",
        "| --- | --- | --- |",
        _rate_row("Scenarios", before["metrics"], after["metrics"],
                  "scenario_pass_rate"),
        _rate_row("Assertions", before["metrics"], after["metrics"],
                  "assertion_pass_rate"),
        "| Critical failures | "
        f"{before['metrics'].get('critical_failures', 0)} | "
        f"{after['metrics'].get('critical_failures', 0)} |",
        _score_row("Quality (mean rubric)", before["metrics"],
                   after["metrics"]),
        _metric_row("Model time (sum)", before["metrics"], after["metrics"],
                    "latency_ms", _duration_cell),
        _metric_row("Model latency (mean)", before["metrics"],
                    after["metrics"], "latency_mean_ms", _duration_cell),
        _metric_row("Completion tokens/s", before["metrics"], after["metrics"],
                    "tokens_per_second", _number_cell),
        "",
    ]
    _section(lines, "Regressions", comparison["regressions"])
    _section(lines, "Improvements", comparison["improvements"])
    if comparison["added"]:
        lines.extend(["## Added", ""])
        lines.extend(f"- `{key}`" for key in comparison["added"])
        lines.append("")
    if comparison["removed"]:
        lines.extend(["## Removed", ""])
        lines.extend(f"- `{key}`" for key in comparison["removed"])
        lines.append("")
    lines.extend([
        "## Unchanged",
        "",
        f"{len(comparison['unchanged'])} scenario repeat(s) unchanged.",
        "",
    ])
    return "\n".join(lines)


def _index(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for scenario in report.get("scenarios") or []:
        key = f"{scenario['scenario_id']}@r{scenario.get('repeat', 1)}"
        out[key] = scenario
    return out


def _change(key: str, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    old_failures = _failed_assertions(old)
    new_failures = _failed_assertions(new)
    return {
        "scenario": key,
        "before_status": old.get("status"),
        "after_status": new.get("status"),
        "before_failures": old_failures,
        "after_failures": new_failures,
    }


def _failed_assertions(scenario: dict[str, Any]) -> list[str]:
    return [
        assertion["id"]
        for assertion in scenario.get("assertions") or []
        if assertion.get("status") == "failed"
    ]


def _section(lines: list[str], title: str, items: list[dict[str, Any]]) -> None:
    lines.extend([f"## {title}", ""])
    if not items:
        lines.extend(["None.", ""])
        return
    for item in items:
        lines.append(
            f"- `{item['scenario']}`: {item['before_status']} -> "
            f"{item['after_status']}"
        )
        for assertion in item.get("after_failures") or []:
            lines.append(f"  - now failing: `{assertion}`")
        for assertion in item.get("before_failures") or []:
            if assertion not in (item.get("after_failures") or []):
                lines.append(f"  - fixed: `{assertion}`")
    lines.append("")


def _rate_row(
    label: str, before: dict[str, Any], after: dict[str, Any], key: str
) -> str:
    return f"| {label} | {_rate_cell(before.get(key))} | {_rate_cell(after.get(key))} |"


def _rate_cell(rate: dict[str, Any] | None) -> str:
    if not rate:
        return "n/a"
    value = rate.get("value")
    percent = f"{value * 100:.1f}%" if value is not None else "n/a"
    return f"{rate.get('numerator', 0)}/{rate.get('denominator', 0)} ({percent})"


def _score_row(label: str, before: dict[str, Any], after: dict[str, Any]) -> str:
    return (
        f"| {label} | {_score_cell((before.get('quality') or {}).get('mean_score'))} "
        f"| {_score_cell((after.get('quality') or {}).get('mean_score'))} |"
    )


def _score_cell(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _metric_row(
    label: str,
    before: dict[str, Any],
    after: dict[str, Any],
    key: str,
    formatter,
) -> str:
    return (
        f"| {label} | {formatter((before.get('model') or {}).get(key))} "
        f"| {formatter((after.get('model') or {}).get(key))} |"
    )


def _duration_cell(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return str(value)
    if milliseconds >= 1000:
        return f"{milliseconds / 1000:.1f} s"
    return f"{milliseconds:.0f} ms"


def _number_cell(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return str(value)


def metrics_from_scenarios(report: dict[str, Any]) -> dict[str, Any]:
    """Fallback for reports written before metrics were embedded."""
    scenarios = report.get("scenarios") or []
    assertions = [
        assertion
        for scenario in scenarios
        for step in scenario.get("steps") or []
        for assertion in step.get("assertions") or []
    ]
    by_status: dict[str, int] = {}
    for scenario in scenarios:
        status = scenario.get("status") or "error"
        by_status[status] = by_status.get(status, 0) + 1
    assertion_status: dict[str, int] = {}
    for assertion in assertions:
        status = assertion.get("status") or "failed"
        assertion_status[status] = assertion_status.get(status, 0) + 1
    scores: list[float] = []
    quality_criteria = 0
    quality_passed = 0
    model_totals: dict[str, int] = {
        "calls": 0, "runs": 0, "latency_ms": 0, "prompt_tokens": 0,
        "completion_tokens": 0, "iterations": 0, "tool_calls": 0,
    }
    have_prompt = have_completion = False
    for scenario in scenarios:
        for step in scenario.get("steps") or []:
            quality = step.get("quality") or {}
            score = quality.get("score")
            if isinstance(score, (int, float)):
                scores.append(float(score))
            for criterion in quality.get("criteria") or []:
                quality_criteria += 1
                if criterion.get("status") == "passed":
                    quality_passed += 1
        model_metrics = scenario.get("model") or {}
        if not model_metrics:
            model_metrics = _steps_model_fallback(scenario.get("steps") or [])
        for key in ("calls", "runs", "latency_ms", "iterations", "tool_calls"):
            model_totals[key] += int(model_metrics.get(key) or 0)
        if model_metrics.get("prompt_tokens") is not None:
            model_totals["prompt_tokens"] += int(model_metrics["prompt_tokens"])
            have_prompt = True
        if model_metrics.get("completion_tokens") is not None:
            model_totals["completion_tokens"] += int(
                model_metrics["completion_tokens"]
            )
            have_completion = True
    categories: dict[str, dict[str, int]] = {}
    for scenario in scenarios:
        bucket = categories.setdefault(
            scenario.get("category") or "uncategorized",
            {"scenarios": 0, "passed": 0, "assertions": 0,
             "assertions_passed": 0},
        )
        bucket["scenarios"] += 1
        if scenario.get("status") == "passed":
            bucket["passed"] += 1
        step_assertions = [
            assertion
            for step in scenario.get("steps") or []
            for assertion in step.get("assertions") or []
        ]
        bucket["assertions"] += len(step_assertions)
        bucket["assertions_passed"] += sum(
            1 for assertion in step_assertions
            if assertion.get("status") == "passed"
        )

    def rate(numerator: int, denominator: int) -> dict[str, Any]:
        return {
            "numerator": numerator,
            "denominator": denominator,
            "value": round(numerator / denominator, 4) if denominator else None,
        }

    return {
        "scenarios": len(scenarios),
        "scenarios_by_status": by_status,
        "scenario_pass_rate": rate(by_status.get("passed", 0), len(scenarios)),
        "assertions": len(assertions),
        "assertions_by_status": assertion_status,
        "assertion_pass_rate": rate(
            assertion_status.get("passed", 0), len(assertions)
        ),
        "critical_failures": len([
            assertion for assertion in assertions
            if assertion.get("status") == "failed"
            and assertion.get("severity") == "critical"
        ]),
        "quality": {
            "scores": scores,
            "mean_score": (
                round(sum(scores) / len(scores), 4) if scores else None
            ),
            "criteria": quality_criteria,
            "criteria_passed": quality_passed,
            "criterion_pass_rate": rate(quality_passed, quality_criteria),
        },
        "model": {
            **model_totals,
            "prompt_tokens": (
                model_totals["prompt_tokens"] if have_prompt else None
            ),
            "completion_tokens": (
                model_totals["completion_tokens"] if have_completion else None
            ),
            "total_tokens": (
                model_totals["prompt_tokens"]
                + model_totals["completion_tokens"]
                if (have_prompt or have_completion) else None
            ),
            "latency_mean_ms": (
                round(model_totals["latency_ms"] / model_totals["calls"])
                if model_totals["calls"] else None
            ),
            "tokens_per_second": (
                round(
                    model_totals["completion_tokens"]
                    / (model_totals["latency_ms"] / 1000),
                    1,
                )
                if have_completion and model_totals["latency_ms"] > 0 else None
            ),
        },
        "categories": categories,
        "failures": [
            failure
            for scenario in scenarios
            for step in scenario.get("steps") or []
            for failure in [
                {
                    "scenario_id": scenario.get("scenario_id"),
                    "repeat": scenario.get("repeat", 1),
                    "step": step.get("name"),
                    "assertion": assertion.get("id"),
                    "expected": assertion.get("expected"),
                    "observed": assertion.get("observed"),
                    "message": assertion.get("message"),
                    "severity": assertion.get("severity"),
                }
                for assertion in step.get("assertions") or []
                if assertion.get("status") == "failed"
            ]
        ],
    }


def _steps_model_fallback(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Reconstruct model totals for reports written before scenario totals."""
    calls = latency = prompt = completion = iterations = tool_calls = runs = 0
    have_prompt = have_completion = False
    for step in steps:
        metrics = step.get("model") or {}
        calls += int(metrics.get("calls") or 0)
        latency += int(metrics.get("latency_ms") or 0)
        runs += int(metrics.get("runs") or 0)
        iterations += int(metrics.get("iterations") or 0)
        tool_calls += int(metrics.get("tool_calls") or 0)
        if metrics.get("prompt_tokens") is not None:
            prompt += int(metrics["prompt_tokens"])
            have_prompt = True
        if metrics.get("completion_tokens") is not None:
            completion += int(metrics["completion_tokens"])
            have_completion = True
    if not calls and not runs:
        return {}
    return {
        "calls": calls,
        "runs": runs,
        "latency_ms": latency,
        "prompt_tokens": prompt if have_prompt else None,
        "completion_tokens": completion if have_completion else None,
        "iterations": iterations,
        "tool_calls": tool_calls,
    }

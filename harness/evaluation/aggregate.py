"""Aggregate several evaluation reports into per-model distributions.

One report answers "what happened in this run". A model choice needs "what
happens across runs, and where does it differ per scenario". This module groups
reports by model and reports:

- scenario and assertion pass rates over all repeats
- rubric score distributions and per-criterion failure counts
- model-only latency, token throughput, iterations, and tool calls
- a per-scenario table that shows where each model is fast or weak

Latency here is the recorded ``model.call`` time, not scenario wall time, so
tool execution and harness overhead do not masquerade as model speed.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from .report import load_run


def resolve_report_paths(
    patterns: list[str],
    results_dir: Path | str,
) -> list[Path]:
    """Resolve tags, report paths, or globs to unique JSON report paths."""
    root = Path(results_dir)
    found: list[Path] = []
    for pattern in patterns:
        candidate = Path(pattern)
        if candidate.suffix == ".json" and candidate.is_file():
            found.append(candidate)
            continue
        matches = sorted(root.glob(f"{pattern}.json"))
        if not matches:
            matches = sorted(root.glob(pattern))
        if not matches:
            raise FileNotFoundError(
                f"no evaluation report matched {pattern!r} in {root}"
            )
        found.extend(matches)
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in found:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def load_reports(
    patterns: list[str], results_dir: Path | str
) -> list[dict[str, Any]]:
    return [load_run(str(path), results_dir) for path in
            resolve_report_paths(patterns, results_dir)]


def aggregate_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not reports:
        raise ValueError("no reports to aggregate")
    models: dict[str, dict[str, Any]] = {}
    for report in reports:
        name = str((report.get("model") or {}).get("name") or "unknown")
        bucket = models.setdefault(name, _new_model(name))
        _add_report(bucket, report)
    for bucket in models.values():
        _finalize_model(bucket)
    return {
        "models": models,
        "reports": [
            {
                "tag": report.get("tag"),
                "run_id": report.get("run_id"),
                "model": (report.get("model") or {}).get("name"),
                "mode": report.get("mode"),
                "started_at": report.get("started_at"),
            }
            for report in reports
        ],
    }


def _new_model(name: str) -> dict[str, Any]:
    return {
        "model": name,
        "reports": 0,
        "runs": 0,
        "scenarios": {},
        "scenario_passed": 0,
        "assertions_total": 0,
        "assertions_passed": 0,
        "critical_failures": 0,
        "quality_scores": [],
        "criterion_total": 0,
        "criterion_passed": 0,
        "criterion_failures": {},
        "model_latency_ms": [],
        "wall_ms": [],
        "tokens_per_second": [],
        "iterations": [],
        "tool_calls": [],
        "tags": [],
    }


def _add_report(bucket: dict[str, Any], report: dict[str, Any]) -> None:
    bucket["reports"] += 1
    bucket["tags"].append(str(report.get("tag") or report.get("run_id") or "?"))
    for scenario in report.get("scenarios") or []:
        scenario_id = str(scenario.get("scenario_id") or "unknown")
        record = bucket["scenarios"].setdefault(scenario_id, {
            "scenario_id": scenario_id,
            "category": scenario.get("category") or "uncategorized",
            "runs": 0,
            "passed": 0,
            "statuses": {},
            "quality_scores": [],
            "model_latency_ms": [],
            "wall_ms": [],
            "tokens_per_second": [],
            "iterations": [],
            "tool_calls": [],
            "criterion_failures": {},
            "criterion_total": {},
        })
        bucket["runs"] += 1
        record["runs"] += 1
        status = scenario.get("status") or "error"
        record["statuses"][status] = record["statuses"].get(status, 0) + 1
        if status == "passed":
            record["passed"] += 1
            bucket["scenario_passed"] += 1

        model_metrics = scenario.get("model") or {}
        if not model_metrics:
            model_metrics = _scenario_model_fallback(scenario)
        latency = model_metrics.get("latency_ms")
        if latency is not None:
            record["model_latency_ms"].append(int(latency))
            bucket["model_latency_ms"].append(int(latency))
        wall = scenario.get("duration_ms")
        if wall is not None:
            record["wall_ms"].append(int(wall))
            bucket["wall_ms"].append(int(wall))
        tps = model_metrics.get("tokens_per_second")
        if tps is not None:
            record["tokens_per_second"].append(float(tps))
            bucket["tokens_per_second"].append(float(tps))
        iterations = model_metrics.get("iterations")
        if iterations is not None:
            record["iterations"].append(int(iterations))
            bucket["iterations"].append(int(iterations))
        tool_calls = model_metrics.get("tool_calls")
        if tool_calls is not None:
            record["tool_calls"].append(int(tool_calls))
            bucket["tool_calls"].append(int(tool_calls))

        assertions = scenario.get("assertions") or []
        bucket["assertions_total"] += len(assertions)
        bucket["assertions_passed"] += sum(
            1 for item in assertions if item.get("status") == "passed"
        )
        bucket["critical_failures"] += sum(
            1 for item in assertions
            if item.get("status") == "failed"
            and item.get("severity") == "critical"
        )

        run_scores: list[float] = []
        for step in scenario.get("steps") or []:
            quality = step.get("quality") or {}
            score = quality.get("score")
            if isinstance(score, (int, float)):
                run_scores.append(float(score))
                bucket["quality_scores"].append(float(score))
            for criterion in quality.get("criteria") or []:
                criterion_id = str(criterion.get("id") or "criterion")
                bucket["criterion_total"] += 1
                record["criterion_total"][criterion_id] = (
                    record["criterion_total"].get(criterion_id, 0) + 1
                )
                if criterion.get("status") == "passed":
                    bucket["criterion_passed"] += 1
                else:
                    bucket["criterion_failures"][criterion_id] = (
                        bucket["criterion_failures"].get(criterion_id, 0) + 1
                    )
                    record["criterion_failures"][criterion_id] = (
                        record["criterion_failures"].get(criterion_id, 0) + 1
                    )
        if run_scores:
            record["quality_scores"].append(
                sum(run_scores) / len(run_scores)
            )


def _finalize_model(bucket: dict[str, Any]) -> None:
    bucket["scenario_pass_rate"] = _rate(
        bucket["scenario_passed"], bucket["runs"]
    )
    bucket["assertion_pass_rate"] = _rate(
        bucket["assertions_passed"], bucket["assertions_total"]
    )
    bucket["criterion_pass_rate"] = _rate(
        bucket["criterion_passed"], bucket["criterion_total"]
    )
    bucket["quality_mean"] = _mean(bucket["quality_scores"])
    bucket["quality"] = _stats(bucket["quality_scores"])
    bucket["model_ms"] = _stats(bucket["model_latency_ms"])
    bucket["wall_ms_stats"] = _stats(bucket["wall_ms"])
    bucket["tokens_per_second_stats"] = _stats(bucket["tokens_per_second"])
    bucket["iterations_stats"] = _stats(bucket["iterations"])
    bucket["tool_calls_stats"] = _stats(bucket["tool_calls"])
    bucket["model_ms_total"] = sum(bucket["model_latency_ms"])
    for record in bucket["scenarios"].values():
        record["scenario_pass_rate"] = _rate(
            record["passed"], record["runs"]
        )
        record["quality_mean"] = _mean(record["quality_scores"])
        record["quality"] = _stats(record["quality_scores"])
        record["model_ms"] = _stats(record["model_latency_ms"])
        record["wall_ms_stats"] = _stats(record["wall_ms"])
        record["tokens_per_second_stats"] = _stats(
            record["tokens_per_second"]
        )
        record["iterations_stats"] = _stats(record["iterations"])
        record["tool_calls_stats"] = _stats(record["tool_calls"])


def _scenario_model_fallback(scenario: dict[str, Any]) -> dict[str, Any]:
    calls = latency = iterations = tool_calls = 0
    completion = 0
    have_completion = False
    for step in scenario.get("steps") or []:
        metrics = step.get("model") or {}
        calls += int(metrics.get("calls") or 0)
        latency += int(metrics.get("latency_ms") or 0)
        iterations += int(metrics.get("iterations") or 0)
        tool_calls += int(metrics.get("tool_calls") or 0)
        if metrics.get("completion_tokens") is not None:
            completion += int(metrics["completion_tokens"])
            have_completion = True
    return {
        "calls": calls,
        "latency_ms": latency,
        "iterations": iterations,
        "tool_calls": tool_calls,
        "tokens_per_second": (
            round(completion / (latency / 1000), 1)
            if have_completion and latency > 0 else None
        ),
    }


def render_aggregate(aggregation: dict[str, Any]) -> str:
    models = aggregation["models"]
    names = sorted(models)
    lines = [
        "# Aggregate model comparison",
        "",
        f"- Reports: {len(aggregation['reports'])}",
        f"- Models: {', '.join(f'`{name}`' for name in names)}",
        "",
        "## Overall",
        "",
        "| Metric | " + " | ".join(names) + " |",
        "| --- | " + " | ".join("---:" for _ in names) + " |",
    ]
    rows = [
        ("Reports", lambda m: str(m["reports"])),
        ("Scenario repeats", lambda m: str(m["runs"])),
        ("Scenario pass", lambda m: _rate_text(m["scenario_pass_rate"])),
        ("Assertion pass", lambda m: _rate_text(m["assertion_pass_rate"])),
        ("Critical failures", lambda m: str(m["critical_failures"])),
        ("Quality mean", lambda m: _score_text(m["quality_mean"])),
        ("Rubric criteria", lambda m: _rate_text(m["criterion_pass_rate"])),
        ("Model latency (mean/run)", lambda m: _ms_text(
            m["model_ms"].get("mean"))),
        ("Model time (sum)", lambda m: _ms_text(m["model_ms_total"])),
        ("Completion tokens/s", lambda m: _number_text(
            m["tokens_per_second_stats"].get("mean"))),
        ("Iterations (mean/run)", lambda m: _number_text(
            m["iterations_stats"].get("mean"))),
        ("Tool calls (mean/run)", lambda m: _number_text(
            m["tool_calls_stats"].get("mean"))),
        ("Wall time (mean/run)", lambda m: _ms_text(
            m["wall_ms_stats"].get("mean"))),
    ]
    for label, getter in rows:
        lines.append(
            f"| {label} | " + " | ".join(getter(models[name]) for name in names)
            + " |"
        )
    lines.append("")

    lines.extend([
        "## Per scenario",
        "",
        "| Scenario | Model | Runs | Pass | Quality | Model ms | Wall ms "
        "| Tokens/s | Iter | Tools |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    scenario_ids = sorted({
        scenario_id
        for name in names
        for scenario_id in models[name]["scenarios"]
    })
    for scenario_id in scenario_ids:
        for name in names:
            record = models[name]["scenarios"].get(scenario_id)
            if record is None:
                continue
            lines.append(
                f"| `{scenario_id}` | `{name}` | {record['runs']} "
                f"| {_rate_text(record['scenario_pass_rate'])} "
                f"| {_score_text(record['quality_mean'])} "
                f"| {_ms_text(record['model_ms'].get('mean'))} "
                f"| {_ms_text(record['wall_ms_stats'].get('mean'))} "
                f"| {_number_text(record['tokens_per_second_stats'].get('mean'))} "
                f"| {_number_text(record['iterations_stats'].get('mean'))} "
                f"| {_number_text(record['tool_calls_stats'].get('mean'))} |"
            )
    lines.append("")

    failures = {
        name: models[name]["criterion_failures"] for name in names
        if models[name]["criterion_failures"]
    }
    if failures:
        lines.extend([
            "## Rubric criterion failures",
            "",
            "| Model | Criterion | Failed runs |",
            "| --- | --- | ---: |",
        ])
        for name in names:
            for criterion, count in sorted(failures.get(name, {}).items()):
                lines.append(f"| `{name}` | `{criterion}` | {count} |")
        lines.append("")
    return "\n".join(lines)


def write_aggregate(
    aggregation: dict[str, Any], results_dir: Path | str
) -> tuple[Path, Path]:
    root = Path(results_dir)
    root.mkdir(parents=True, exist_ok=True)
    names = sorted(aggregation["models"])
    stem = "aggregate_" + "_vs_".join(_safe(name) for name in names)
    json_path = root / f"{stem}.json"
    md_path = root / f"{stem}.md"
    json_path.write_text(
        json.dumps(aggregation, indent=2, default=str), encoding="utf-8"
    )
    md_path.write_text(render_aggregate(aggregation), encoding="utf-8")
    return json_path, md_path


def _safe(text: str) -> str:
    return "".join(
        char if char.isalnum() or char in "-_." else "_" for char in str(text)
    )


def _stats(values: list[float]) -> dict[str, Any]:
    numbers = [float(value) for value in values]
    if not numbers:
        return {"n": 0}
    return {
        "n": len(numbers),
        "mean": round(statistics.mean(numbers), 1),
        "median": round(statistics.median(numbers), 1),
        "min": round(min(numbers), 1),
        "max": round(max(numbers), 1),
        "stdev": (
            round(statistics.pstdev(numbers), 1) if len(numbers) > 1 else 0.0
        ),
    }


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 4) if values else None


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": round(numerator / denominator, 4) if denominator else None,
    }


def _rate_text(rate: dict[str, Any]) -> str:
    value = rate.get("value")
    percent = f"{value * 100:.1f}%" if value is not None else "n/a"
    return f"{rate.get('numerator', 0)}/{rate.get('denominator', 0)} ({percent})"


def _score_text(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _ms_text(value: Any) -> str:
    if value is None:
        return "n/a"
    milliseconds = float(value)
    if milliseconds >= 1000:
        return f"{milliseconds / 1000:.1f} s"
    return f"{milliseconds:.0f} ms"


def _number_text(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.1f}"

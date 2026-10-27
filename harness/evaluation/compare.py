"""Compare two persisted evaluation reports.

The loader accepts the wrapper written by :mod:`harness.evaluation.report`
(``{"run": ..., "summary": ...}``) and a bare run document for portability.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.evaluation.metrics import compare_summaries


def load_report(path: str | Path) -> dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path}: expected a JSON object")
    if isinstance(document.get("run"), dict):
        run = dict(document["run"])
        summary = document.get("summary")
        if isinstance(summary, dict):
            run["summary"] = summary
        return run
    return document


def compare_reports(left: str | Path, right: str | Path) -> dict[str, Any]:
    return compare_summaries(load_report(left), load_report(right))


def render_comparison(comparison: dict[str, Any]) -> str:
    lines = [
        f"Comparison: {comparison['left_tag']} ({comparison['left_mode']}) "
        f"vs {comparison['right_tag']} ({comparison['right_mode']})",
        f"Runs: {comparison['left_run_id']} -> {comparison['right_run_id']}",
        "",
        f"Regressions: {len(comparison['regressions'])}",
        f"Improvements: {len(comparison['improvements'])}",
        f"Unchanged: {len(comparison['unchanged'])}",
    ]
    for row in comparison["regressions"]:
        lines.append(
            f"  REGRESSION {row['scenario_id']} #{row['repeat']}: "
            f"{row['before']} -> {row['after']} ({row.get('reason', '')})"
        )
    for row in comparison["improvements"]:
        lines.append(
            f"  IMPROVED   {row['scenario_id']} #{row['repeat']}: "
            f"{row['before']} -> {row['after']} ({row.get('reason', '')})"
        )
    if comparison["latency_changes"]:
        lines.append("")
        lines.append("Latency changes:")
        for row in comparison["latency_changes"]:
            sign = "+" if row["delta_ms"] >= 0 else ""
            lines.append(
                f"  {row['scenario_id']} #{row['repeat']}: "
                f"{row['before_ms']} -> {row['after_ms']} ms ({sign}{row['delta_ms']})"
            )
    interesting = {
        name: row
        for name, row in comparison["metric_changes"].items()
        if row.get("delta") not in (None, 0)
    }
    if interesting:
        lines.append("")
        lines.append("Metric changes (pass rate):")
        for name, row in interesting.items():
            lines.append(
                f"  {name}: {row['before']} -> {row['after']} "
                f"(n={row['denominator_before']} -> {row['denominator_after']})"
            )
    safety = comparison["critical_safety_failures"]
    lines.append("")
    lines.append(
        "Critical safety failures: "
        f"left={len(safety['left'])}, right={len(safety['right'])}"
    )
    for side in ("left", "right"):
        for row in safety[side]:
            lines.append(
                f"  {side.upper()} {row['scenario_id']} #{row['repeat']}: {row['assertion_id']}"
            )
    return "\n".join(lines)


__all__ = ["compare_reports", "load_report", "render_comparison"]

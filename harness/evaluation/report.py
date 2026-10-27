"""Write machine-readable and human-readable evaluation reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.evaluation.models import EvaluationRun
from harness.evaluation.runner import _redact
from harness.evaluation.safety import MARKER


def _clip(value: Any, limit: int = 500) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.replace("\r", "").replace("\n", " ")
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _rate(value: dict[str, Any] | None, name: str) -> str:
    if not isinstance(value, dict) or value.get("rate") is None:
        return f"{name}: n/a (0 measured)"
    return (
        f"{name}: {value['rate']:.1%} "
        f"({value.get('numerator', 0)}/{value.get('denominator', 0)})"
    )


def render_markdown(run: EvaluationRun, summary: dict[str, Any]) -> str:
    config = run.config or {}
    lines: list[str] = [
        f"# Evaluation report: {run.tag}",
        "",
        "## Run",
        "",
        f"- Run ID: `{run.run_id}`",
        f"- Mode: **{run.mode}**",
        f"- Started: {run.started_at}",
        f"- Finished: {run.finished_at}",
        f"- Sandbox: `{run.sandbox_root}`",
        f"- Results directory: `{run.results_dir or ''}`",
        f"- Model: `{config.get('model')}`",
        f"- Endpoint: `{config.get('endpoint')}`",
        f"- Prompt variant: `{config.get('prompt_variant')}`",
        f"- Planning mode: `{config.get('planning_mode')}`",
        f"- Temperature / max tokens: `{config.get('temperature')}` / `{config.get('max_tokens')}`",
        f"- Endpoint probe: `{'ok' if run.endpoint_ok else ('failed' if run.endpoint_ok is False else 'not applicable')}`",
    ]
    if run.endpoint_error:
        lines.append(f"- Endpoint error: {run.endpoint_error}")
    if run.notes:
        lines.append("- Notes: " + "; ".join(run.notes))
    lines.extend(
        [
            "",
            "## Summary",
            "",
            "| Measure | Value |",
            "| --- | --- |",
            f"| Scenario runs | {summary['scenario_runs']} |",
            f"| Scenario pass rate | {_rate(summary['scenario_pass_rate'], 'passed')} |",
            f"| Passed / failed / inconclusive / skipped | {summary['passed']} / "
            f"{summary['failed']} / {summary['inconclusive']} / {summary['skipped']} |",
            f"| Assertion pass rate | {_rate(summary['assertion_pass_rate'], 'passed')} |",
            f"| Assertions passed / failed / inconclusive | {summary['assertions_passed']} / "
            f"{summary['assertions_failed']} / {summary['assertions_inconclusive']} |",
            f"| Critical failures | {summary['critical_failures']} |",
            f"| Unauthorized dispatches | {summary['unauthorized_dispatch']} |",
            f"| Turn latency samples | {summary['latency']['samples']} |",
            f"| Latency p50 / p95 / max | {summary['latency']['p50_ms']} / "
            f"{summary['latency']['p95_ms']} / {summary['latency']['max_ms']} ms |",
            "",
            "### Named metrics",
            "",
            "| Metric | Result | Samples | Basis |",
            "| --- | --- | --- | --- |",
        ]
    )
    named = [
        ("Decision interpretation accuracy", "decision_interpretation_accuracy", "higher is better"),
        ("Grounded recall accuracy", "grounded_recall_accuracy", "higher is better"),
        ("Grounding accuracy", "grounding_accuracy", "higher is better"),
        ("Memory-scope accuracy", "memory_scope_accuracy", "higher is better"),
        ("Project-association accuracy", "project_association_accuracy", "higher is better"),
        ("Context-retrieval success", "context_retrieval_success_rate", "higher is better"),
        ("Task-recovery correctness", "task_recovery_correctness", "higher is better"),
        ("Learning-lifecycle correctness", "learning_lifecycle_correctness", "higher is better"),
        ("Clarification on ambiguity", "clarification_rate_on_ambiguous", "higher is better"),
        ("Unsupported-claim rate", "unsupported_claim_rate", "lower is better"),
        ("Incorrect-approval rate", "incorrect_approval_rate", "lower is better"),
        ("Unauthorized-dispatch rate", "unauthorized_dispatch_rate", "lower is better"),
    ]
    for label, key, basis in named:
        value = summary.get(key) or {}
        lines.append(
            f"| {label} | {_rate(value, 'rate')} | {value.get('denominator', 0)} | {basis} |"
        )
    lines.append(f"| Unauthorized dispatches (count) | {summary['unauthorized_dispatch']} | - | lower is better |")

    lines.extend(
        [
            "",
            "### By category",
            "",
            "| Category | Scenarios | Passed | Failed | Inconclusive | Pass rate | Assertions | Critical |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for name, block in summary["by_category"].items():
        lines.append(
            f"| {name} | {block['scenarios']} | {block['passed']} | {block['failed']} | "
            f"{block['inconclusive']} | {_rate(block['scenario_pass_rate'], 'pass')} | "
            f"{block['assertions_passed']}/{block['assertions']} | {block['critical_failures']} |"
        )
    lines.extend(
        [
            "",
            "### By severity",
            "",
            "| Severity | Scenarios | Passed | Failed | Inconclusive | Pass rate | Assertions | Critical |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for name, block in summary["by_severity"].items():
        lines.append(
            f"| {name} | {block['scenarios']} | {block['passed']} | {block['failed']} | "
            f"{block['inconclusive']} | {_rate(block['scenario_pass_rate'], 'pass')} | "
            f"{block['assertions_passed']}/{block['assertions']} | {block['critical_failures']} |"
        )

    lines.extend(["", "## Scenarios", "", "| Scenario | Category | Severity | Repeat | Status | Duration |", "| --- | --- | --- | --- | --- | --- |"])
    for scenario in run.scenarios:
        lines.append(
            f"| `{scenario.scenario_id}` | {scenario.category} | {scenario.severity} | "
            f"{scenario.repeat} | **{scenario.status}** | {scenario.duration_ms} ms |"
        )

    if summary["failure_rows"]:
        lines.extend(["", "## Failures and inconclusive results", ""])
        for row in summary["failure_rows"]:
            lines.append(
                f"### `{row['scenario_id']}` repeat {row['repeat']} — {row['status']}"
            )
            lines.append("")
            lines.append(f"- Category: {row['category']} / severity: {row['severity']}")
            lines.append(f"- Reason: {row['reason']}")
            lines.append(f"- Run directory: `{row['run_dir']}`")
            lines.append("")

    lines.extend(["", "## Assertion evidence", ""])
    for scenario in run.scenarios:
        lines.append(f"### `{scenario.scenario_id}` repeat {scenario.repeat} ({scenario.status})")
        lines.append("")
        if scenario.error:
            lines.append(f"- Scenario error: {scenario.error}")
        for item in scenario.assertions:
            marker = {
                "passed": "PASS",
                "failed": "FAIL",
                "inconclusive": "INCONCLUSIVE",
                "skipped": "SKIP",
            }.get(item.status, item.status.upper())
            lines.append(
                f"- **{marker}** `{item.id}` ({item.type}, {item.severity}) — {item.description}"
            )
            lines.append(f"  - expected: {_clip(item.expected)}")
            lines.append(f"  - observed: {_clip(item.observed)}")
            if item.message:
                lines.append(f"  - note: {_clip(item.message)}")
            if item.evidence_refs:
                lines.append(f"  - evidence: {_clip(item.evidence_refs, 300)}")
        lines.append("")

    lines.extend(
        [
            "",
            "## Interpretation limits",
            "",
            "- A pass means the declared assertions found matching persisted evidence. It is "
            "not a claim of general intelligence.",
            "- Assertions marked inconclusive had missing evidence and never count as passes.",
            "- Any unauthorized dispatch is a critical failure even when the overall rate is high.",
            "- Live-model results vary between runs; use repeats and compare reports rather "
            "than treating one sample as proof.",
            f"- Sandbox runs are marked with `{MARKER}` and must never point at production data.",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(
    run: EvaluationRun,
    summary: dict[str, Any],
    results_dir: str | Path,
) -> dict[str, Path]:
    root = Path(results_dir)
    root.mkdir(parents=True, exist_ok=True)
    safe_tag = run.tag
    json_path = root / f"{safe_tag}.json"
    markdown_path = root / f"{safe_tag}.md"
    payload = {
        "format_version": 1,
        "run": _redact(run.as_dict()),
        "summary": _redact(summary),
    }
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    markdown_path.write_text(
        render_markdown(run, summary),
        encoding="utf-8",
        newline="\n",
    )
    run.results_dir = str(root)
    return {"json": json_path, "markdown": markdown_path}


__all__ = ["render_markdown", "write_reports"]

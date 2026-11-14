"""JSON and Markdown reports for evaluation runs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import EvaluationRun, metrics

_SAFE_TAG = re.compile(r"[^A-Za-z0-9_.-]")


def write_reports(
    run: EvaluationRun, results_dir: Path | str
) -> tuple[Path, Path]:
    root = Path(results_dir)
    root.mkdir(parents=True, exist_ok=True)
    tag = _SAFE_TAG.sub("_", run.tag) or run.run_id
    json_path = root / f"{tag}.json"
    md_path = root / f"{tag}.md"
    json_path.write_text(
        json.dumps(run.as_dict(), indent=2, default=str),
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(run), encoding="utf-8")
    return json_path, md_path


def load_run(
    tag_or_path: str, results_dir: Path | str = Path("data") / "eval" / "results"
) -> dict[str, Any]:
    candidate = Path(tag_or_path)
    if candidate.suffix == ".json" and candidate.is_file():
        path = candidate
    else:
        path = Path(results_dir) / f"{tag_or_path}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no evaluation report at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def render_markdown(run: EvaluationRun) -> str:
    data = run.as_dict()
    counts = data["metrics"]
    model = data.get("model") or {}
    lines = [
        f"# Evaluation: {run.tag}",
        "",
        f"- Run ID: `{run.run_id}`",
        f"- Mode: `{run.mode}`",
        f"- Started: {run.started_at}",
        f"- Finished: {run.finished_at or '-'}",
        f"- Model: `{model.get('name')}` at `{model.get('base_url')}`",
        f"- Thinking: {model.get('thinking')}",
        f"- Max tokens: {model.get('max_tokens')}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        _row("Scenarios", _rate_text(counts["scenario_pass_rate"])),
        _row("Assertions", _rate_text(counts["assertion_pass_rate"])),
        _row("Critical failures", counts["critical_failures"]),
        _row("Quality (mean rubric)", _score_text(
            (counts.get("quality") or {}).get("mean_score")
        )),
        _row("Rubric criteria", _rate_text(
            (counts.get("quality") or {}).get("criterion_pass_rate") or {}
        )),
        _row("Statuses", counts["scenarios_by_status"]),
        "",
    ]
    if data.get("endpoint"):
        endpoint = data["endpoint"]
        lines.extend([
            "## Endpoint probe",
            "",
            f"- Reachable: {endpoint.get('reachable')}",
            f"- Model listed: {endpoint.get('model_listed')}",
            f"- Chat OK: {endpoint.get('chat_ok')}",
            f"- Tool calls OK: {endpoint.get('tool_call_ok')}",
            f"- Detail: {endpoint.get('detail') or '-'}",
            "",
        ])
    categories = counts.get("categories") or {}
    if categories:
        lines.extend([
            "## Categories",
            "",
            "| Category | Scenarios | Passed | Assertions | Assertions passed |",
            "| --- | ---: | ---: | ---: | ---: |",
        ])
        for category, bucket in sorted(categories.items()):
            lines.append(
                f"| {category} | {bucket['scenarios']} | {bucket['passed']} "
                f"| {bucket['assertions']} | {bucket['assertions_passed']} |"
            )
        lines.append("")
    quality = (counts.get("quality") or {}).get("steps") or []
    if quality:
        lines.extend([
            "## Quality (rubric)",
            "",
            "| Scenario | Repeat | Step | Score | Min |",
            "| --- | ---: | --- | ---: | ---: |",
        ])
        for item in quality:
            lines.append(
                f"| `{item['scenario_id']}` | {item['repeat']} "
                f"| {item['step']} | {_score_text(item.get('score'))} "
                f"| {_score_text(item.get('min_score'))} |"
            )
        lines.append("")
    lines.extend([
        "## Scenarios",
        "",
        "| Scenario | Mode | Repeat | Status | Duration |",
        "| --- | --- | ---: | --- | ---: |",
    ])
    for scenario in data["scenarios"]:
        lines.append(
            f"| `{scenario['scenario_id']}` | {scenario['mode']} "
            f"| {scenario['repeat']} | {scenario['status']} "
            f"| {scenario['duration_ms']} ms |"
        )
    lines.append("")

    failures = counts.get("failures") or []
    if failures:
        lines.extend(["## Failures", ""])
        for failure in failures:
            lines.extend([
                f"### {failure['scenario_id']} ({failure['step']})",
                "",
                f"- Check: `{failure['assertion']}`",
                f"- Expected: `{_short(failure['expected'])}`",
                f"- Observed: `{_short(failure['observed'])}`",
                f"- Detail: {failure['message'] or '-'}",
                "",
            ])
    else:
        lines.extend(["## Failures", "", "None.", ""])

    errors = [
        scenario for scenario in data["scenarios"] if scenario.get("error")
    ]
    if errors:
        lines.extend(["## Scenario errors", ""])
        for scenario in errors:
            lines.append(
                f"- `{scenario['scenario_id']}` (repeat {scenario['repeat']}): "
                f"{scenario['error']}"
            )
        lines.append("")

    lines.extend([
        "## Interpretation",
        "",
        "- Assertions read persisted harness state (tasks, runs, memories, "
        "approvals, schedules, inbox, modules, executions), not just reply text.",
        "- Deterministic mode scripts the model; it measures harness behavior, "
        "not language quality or model judgment.",
        "- Live mode measures the named model through the real runtime; it is "
        "not a general capability benchmark.",
        "- Rubric scores are reported separately from assertion pass rates and "
        "do not change scenario status.",
        "",
        "## Configuration",
        "",
        "```yaml",
        _yaml_block(data.get("config") or {}),
        "```",
        "",
    ])
    if data.get("notes"):
        lines.extend(["## Notes", ""])
        lines.extend(f"- {note}" for note in data["notes"])
        lines.append("")
    return "\n".join(lines)


def summary_lines(run: dict[str, Any]) -> list[str]:
    counts = run.get("metrics") or {}
    model = run.get("model") or {}
    lines = [
        f"tag={run.get('tag')} mode={run.get('mode')} "
        f"run={run.get('run_id')}",
        f"model={model.get('name')} endpoint={model.get('base_url')}",
        f"scenarios {_rate_text(counts.get('scenario_pass_rate') or {})} | "
        f"assertions {_rate_text(counts.get('assertion_pass_rate') or {})} | "
        f"critical failures {counts.get('critical_failures', 0)}",
        f"quality mean {_score_text((counts.get('quality') or {}).get('mean_score'))}"
        f" | rubric criteria {_rate_text((counts.get('quality') or {}).get('criterion_pass_rate') or {})}",
    ]
    for bucket, count in sorted(
        (counts.get("scenarios_by_status") or {}).items()
    ):
        lines.append(f"  {bucket}: {count}")
    failures = counts.get("failures") or []
    for failure in failures[:20]:
        lines.append(
            f"  FAIL {failure['scenario_id']} {failure['assertion']}: "
            f"expected {_short(failure['expected'])}, "
            f"observed {_short(failure['observed'])}"
        )
    if len(failures) > 20:
        lines.append(f"  ... {len(failures) - 20} more failures")
    return lines


def _row(label: str, value: Any) -> str:
    return f"| {label} | {value} |"


def _rate_text(rate: dict[str, Any]) -> str:
    numerator = rate.get("numerator", 0)
    denominator = rate.get("denominator", 0)
    value = rate.get("value")
    percent = f"{value * 100:.1f}%" if value is not None else "n/a"
    return f"{numerator}/{denominator} ({percent})"


def _score_text(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _short(value: Any, limit: int = 160) -> str:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _yaml_block(value: Any) -> str:
    import yaml

    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True).rstrip()

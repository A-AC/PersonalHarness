"""Run the fixed evaluation set under each latency experiment.

Usage:
    python scripts/experiments.py --configs before_legacy,after_strict_full,after_strict_trimmed --repeats 2
    python scripts/experiments.py --configs after_strict_full,after_loose_full,after_freeform_full --repeats 1 --only valid_call,missing_arg,memory,real_use,internal_schema,tool_failure

Each config writes only the settings it changes; FileSettingsService materializes
the rest from the catalog. Results land in experiments/results/<tag>.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.eval import run_suite  # noqa: E402
DATA = ROOT / "experiments" / "data"
RESULTS = ROOT / "experiments" / "results"

COMMON: dict[str, Any] = {
    "models.interactive.endpoint": "http://192.168.50.40:8181/v1/chat/completions",
    "models.interactive.model_name": "qwen3.5-9b",
    "models.interactive.thinking_budget_tokens": 64,
    "models.interactive.stream": True,
    "models.interactive.timeout_s": 180,
    "models.background.endpoint": "http://192.168.50.40:8181/v1/chat/completions",
    "models.background.model_name": "qwen3.5-9b",
    "models.background.enable_thinking": False,
    "models.background.stream": True,
    "models.background.timeout_s": 180,
}

# V1.1 catalog defaults, written out explicitly so the history arms differ only
# in their history settings.
STRICT: dict[str, Any] = {
    "capabilities.coerce_optional_arguments": True,
    "capabilities.disclose_enum_values": True,
    "capabilities.reject_placeholders": True,
    "loop.honesty_guard": True,
    "loop.internal_schema_ask_guard": True,
    "context.prompt_variant": "full",
    "context.include_time": True,
    "models.interactive.action_schema": "strict",
    "tasks.planning_mode": "heuristic",
    "loop.stop_after_nonretryable_failure": True,
}

CONFIGS: dict[str, dict[str, Any]] = {
    "baseline": {
        "models.interactive.enable_thinking": True,
        "tasks.planning_mode": "llm",
        "loop.stop_after_nonretryable_failure": False,
    },
    "e1_nothinking": {
        "models.interactive.enable_thinking": False,
        "tasks.planning_mode": "llm",
        "loop.stop_after_nonretryable_failure": False,
    },
    "e1b_budget16": {
        "models.interactive.enable_thinking": True,
        "models.interactive.thinking_budget_tokens": 16,
        "tasks.planning_mode": "llm",
        "loop.stop_after_nonretryable_failure": False,
    },
    "e2_heuristic": {
        "models.interactive.enable_thinking": False,
        "tasks.planning_mode": "heuristic",
        "loop.stop_after_nonretryable_failure": False,
    },
    "e3_retrycap": {
        "models.interactive.enable_thinking": False,
        "tasks.planning_mode": "heuristic",
        "loop.stop_after_nonretryable_failure": True,
    },
    "recommended": {
        "models.interactive.enable_thinking": False,
        "tasks.planning_mode": "heuristic",
        "loop.stop_after_nonretryable_failure": True,
    },
    "recommended_b16": {
        "models.interactive.enable_thinking": True,
        "models.interactive.thinking_budget_tokens": 16,
        "tasks.planning_mode": "heuristic",
        "loop.stop_after_nonretryable_failure": True,
    },
    "recommended_think": {
        "models.interactive.enable_thinking": True,
        "tasks.planning_mode": "heuristic",
        "loop.stop_after_nonretryable_failure": True,
    },
    # V1 behavior approximated with the new switches off, for before/after
    # evidence on the real-use scenarios.
    "before_legacy": {
        "capabilities.coerce_optional_arguments": False,
        "capabilities.disclose_enum_values": False,
        "capabilities.reject_placeholders": False,
        "loop.honesty_guard": False,
        "loop.internal_schema_ask_guard": False,
        "context.prompt_variant": "full",
        "context.include_time": False,
        "models.interactive.action_schema": "loose",
        "tasks.planning_mode": "heuristic",
        "loop.stop_after_nonretryable_failure": True,
    },
    "after_strict_full": {
        "capabilities.coerce_optional_arguments": True,
        "capabilities.disclose_enum_values": True,
        "capabilities.reject_placeholders": True,
        "loop.honesty_guard": True,
        "loop.internal_schema_ask_guard": True,
        "context.prompt_variant": "full",
        "context.include_time": True,
        "models.interactive.action_schema": "strict",
        "tasks.planning_mode": "heuristic",
        "loop.stop_after_nonretryable_failure": True,
    },
    "after_strict_trimmed": {
        "capabilities.coerce_optional_arguments": True,
        "capabilities.disclose_enum_values": True,
        "capabilities.reject_placeholders": True,
        "loop.honesty_guard": True,
        "loop.internal_schema_ask_guard": True,
        "context.prompt_variant": "trimmed",
        "context.include_time": True,
        "models.interactive.action_schema": "strict",
        "tasks.planning_mode": "heuristic",
        "loop.stop_after_nonretryable_failure": True,
    },
    "after_loose_full": {
        "capabilities.coerce_optional_arguments": True,
        "capabilities.disclose_enum_values": True,
        "capabilities.reject_placeholders": True,
        "loop.honesty_guard": True,
        "loop.internal_schema_ask_guard": True,
        "context.prompt_variant": "full",
        "context.include_time": True,
        "models.interactive.action_schema": "loose",
        "tasks.planning_mode": "heuristic",
        "loop.stop_after_nonretryable_failure": True,
    },
    # Catalog defaults only: used for the final verification after the
    # experiment chooses the prompt variant and action schema.
    "final_defaults": {},
    "after_freeform_full": {
        "capabilities.coerce_optional_arguments": True,
        "capabilities.disclose_enum_values": True,
        "capabilities.reject_placeholders": True,
        "loop.honesty_guard": True,
        "loop.internal_schema_ask_guard": True,
        "context.prompt_variant": "full",
        "context.include_time": True,
        "models.interactive.response_format": "text",
        "tasks.planning_mode": "heuristic",
        "loop.stop_after_nonretryable_failure": True,
    },
    # V1.2 history experiments. The eval generates pending summaries between
    # graded turns, so background generation is off to keep the comparison
    # deterministic and to keep summary latency out of turn metrics.
    "history_full": {
        **STRICT,
        "context.history.strategy": "full",
        "context.history.background_summary": False,
    },
    "history_summary": {
        **STRICT,
        "context.history.strategy": "summary",
        "context.history.verbatim_turns": 6,
        "context.history.background_summary": False,
    },
    "history_segment": {
        **STRICT,
        "context.history.strategy": "segment",
        "context.history.verbatim_turns": 6,
        "context.history.segment_gap_hours": 12,
        "context.history.background_summary": False,
    },
}


def _dot_set(tree: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    cursor = tree
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value


def _write_settings(path: Path, values: dict[str, Any]) -> None:
    tree: dict[str, Any] = {}
    for key, value in values.items():
        _dot_set(tree, key, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(tree, sort_keys=False), encoding="utf-8", newline="\n")


def _fmt(value: Any, digits: int = 0) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _row(tag: str, summary: dict[str, Any]) -> str:
    passed, runs = summary["passed"], summary["runs"]
    rate = 100 * passed / runs if runs else 0
    return (
        f"| {tag} | {rate:.0f}% ({passed}/{runs}) | "
        f"{_fmt(summary.get('tool_call_success_rate'), 3)} | "
        f"{_fmt(summary.get('malformed_output_rate'), 3)} | "
        f"{_fmt(summary.get('honesty_rejections'))} | "
        f"{_fmt(summary.get('turn_duration_p50_ms'))} | "
        f"{_fmt(summary.get('turn_duration_p95_ms'))} | "
        f"{_fmt(summary.get('model_call_latency_p50_ms'))} | "
        f"{_fmt(summary.get('model_call_latency_p95_ms'))} | "
        f"{_fmt(summary.get('ttft_p50_ms'))} | "
        f"{_fmt(summary.get('thinking_call_ratio'), 2)} | "
        f"{_fmt(summary.get('mean_prompt_tokens'), 1)} | "
        f"{_fmt(summary.get('mean_completion_tokens'), 1)} | "
        f"{_fmt(summary.get('mean_visible_output_tokens'), 1)} |"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configs", default="baseline,e1_nothinking,e1b_budget16,e2_heuristic,e3_retrycap,recommended_think")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--data-dir", default=str(DATA))
    parser.add_argument("--results-dir", default=str(RESULTS))
    parser.add_argument("--endpoint", default=COMMON["models.interactive.endpoint"])
    parser.add_argument("--model", default=COMMON["models.interactive.model_name"])
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--only", default=None, help="Comma-separated scenario ids/categories, as in the eval CLI.")
    parser.add_argument("--tag-suffix", default="", help="Append to each config tag (keeps prior result files).")
    args = parser.parse_args()
    tags = [item.strip() for item in args.configs.split(",") if item.strip()]
    data_dir = Path(args.data_dir)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    summaries: dict[str, Any] = {}
    for tag in tags:
        if tag not in CONFIGS:
            raise SystemExit(f"unknown config {tag}")
        run_tag = f"{tag}{args.tag_suffix}"
        out = results_dir / f"{run_tag}.json"
        if args.skip_existing and out.exists():
            payload = json.loads(out.read_text(encoding="utf-8"))
        else:
            values = dict(COMMON)
            values["models.interactive.endpoint"] = args.endpoint
            values["models.interactive.model_name"] = args.model
            values.update(CONFIGS[tag])
            _write_settings(data_dir / "settings.yaml", values)
            payload = run_suite(
                data_dir=data_dir,
                repeats=args.repeats,
                only=args.only,
                tag=run_tag,
                results_dir=results_dir,
            )
        summaries[run_tag] = payload
        rows.append(_row(run_tag, payload["summary"]))
        summary_path = results_dir / "summary.json"
        combined = (
            json.loads(summary_path.read_text(encoding="utf-8"))
            if summary_path.exists()
            else {"configs": {}, "summaries": {}}
        )
        combined.setdefault("configs", {}).update(
            {key: value["config"] for key, value in summaries.items()}
        )
        combined.setdefault("summaries", {}).update(
            {key: value["summary"] for key, value in summaries.items()}
        )
        summary_path.write_text(
            json.dumps(combined, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    print("\n| config | eval | tool success | malformed | honesty rejects | turn p50 | turn p95 | call p50 | call p95 | ttft p50 | thinking | prompt | completion | visible |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

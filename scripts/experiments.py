"""Run the fixed evaluation set under each latency experiment.

Usage:
    python scripts/experiments.py --configs baseline,e1_nothinking,e1b_budget16,e2_heuristic,e3_retrycap,recommended_think --repeats 2

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
        f"{_fmt(summary.get('turn_duration_p50_ms'))} | "
        f"{_fmt(summary.get('turn_duration_p95_ms'))} | "
        f"{_fmt(summary.get('model_call_latency_p50_ms'))} | "
        f"{_fmt(summary.get('model_call_latency_p95_ms'))} | "
        f"{_fmt(summary.get('ttft_p50_ms'))} | "
        f"{_fmt(summary.get('thinking_call_ratio'), 2)} | "
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
        out = results_dir / f"{tag}.json"
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
                tag=tag,
                results_dir=results_dir,
            )
        summaries[tag] = payload
        rows.append(_row(tag, payload["summary"]))
        (results_dir / "summary.json").write_text(
            json.dumps(
                {
                    "configs": {key: value["config"] for key, value in summaries.items()},
                    "summaries": {key: value["summary"] for key, value in summaries.items()},
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    print("\n| config | eval | tool success | turn p50 | turn p95 | call p50 | call p95 | ttft p50 | thinking | completion | visible |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

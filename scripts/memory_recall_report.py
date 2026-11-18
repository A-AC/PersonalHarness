"""Cross-domain memory recall baseline report.

For each scenario in ``eval/scenarios/k_memory_recall.yaml`` this builds an
isolated App, applies the scenario fixtures, routes the question, and measures
what ``memory.search`` returns under each ``memory.cross_domain`` mode.

    python scripts/memory_recall_report.py --tag phase10d_memory

Phase 10D compares the hard partition (``off``), explicit cross-domain search
(``search``), and lexical cross-domain context (``boost``). The report is
deterministic: no timestamps and no machine paths.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from harness import util  # noqa: E402
from harness.app import build_app  # noqa: E402
from harness.evaluation.fixtures import apply_fixtures  # noqa: E402
from harness.model import ScriptedModel  # noqa: E402

DEFAULT_CORPUS = ROOT / "eval" / "scenarios" / "k_memory_recall.yaml"
DEFAULT_OUT = ROOT / "eval" / "results"
MODES = ("off", "search", "boost")
TOP_K = 5
CLIP_CHARS = 400


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def _modes(value: str) -> list[str]:
    modes = [part.strip() for part in str(value).split(",") if part.strip()]
    if not modes:
        raise argparse.ArgumentTypeError("expected at least one mode")
    unknown = [mode for mode in modes if mode not in MODES]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown modes {unknown}; choose from {', '.join(MODES)}"
        )
    return modes


def _phrases(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def load_corpus(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    scenarios = data.get("scenarios") if isinstance(data, dict) else data
    if not isinstance(scenarios, list) or not scenarios:
        raise SystemExit(f"{path}: no scenarios found")
    out: list[dict[str, Any]] = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        steps = scenario.get("steps") or []
        if not steps or not isinstance(steps[0], dict):
            raise SystemExit(f"{scenario.get('id')}: missing first step")
        expect = steps[0].get("expect") or {}
        context = expect.get("context") or {}
        out.append({
            "id": str(scenario.get("id") or ""),
            "query": str(steps[0].get("input") or ""),
            "fixtures": scenario.get("fixtures") or {},
            "expected": _phrases(context.get("system_contains")),
            "siblings": _phrases(context.get("system_not_contains")),
        })
    return out


def _found(phrases: Sequence[str], memories: Sequence[dict[str, Any]]) -> bool:
    if not phrases:
        return False
    contents = [str(item.get("content") or "").lower() for item in memories]
    return all(
        any(phrase.lower() in content for content in contents)
        for phrase in phrases
    )


def _leaked(phrases: Sequence[str], memories: Sequence[dict[str, Any]]) -> bool:
    if not phrases:
        return False
    contents = [str(item.get("content") or "").lower() for item in memories]
    return any(
        phrase.lower() in content
        for phrase in phrases
        for content in contents
    )


def _budget_share(app, memories: Sequence[dict[str, Any]]) -> float:
    budget_chars = int(
        app.config.get("context.section_budgets.memories", 1800) or 0
    ) * 4
    if budget_chars <= 0:
        return 0.0
    used = sum(
        len(util.clip(str(item.get("content") or ""), CLIP_CHARS))
        for item in memories
    )
    return round(used / budget_chars, 4)


def measure(
    scenarios: Sequence[dict[str, Any]], modes: Sequence[str],
    *, tag: str = "memory_recall",
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for scenario in scenarios:
        path = Path(tempfile.mkdtemp(prefix="memory_recall_"))
        app = build_app(path, model=ScriptedModel())
        try:
            apply_fixtures(app, scenario["fixtures"])
            routing = app.domains.route(
                scenario["query"], open_tasks=[], recent_messages=[]
            )
            domain_id = str(routing.get("domain_id") or "general")
            record = app.domains.get(domain_id)
            routed_name = str(record["name"]) if record else domain_id
            priority: list[str] = []
            if domain_id != "general":
                priority.append(f"domain:{domain_id}")
            priority.append("global")

            results: dict[str, Any] = {}
            for mode in modes:
                app.config.set("memory.cross_domain", mode)
                # Boost models normal context retrieval. Search models the
                # explicit search_knowledge path, which searches every domain;
                # off remains the hard partition.
                memories = app.memory.search(
                    scenario["query"], scope_priority=list(priority),
                    limit=TOP_K,
                    cross_domain=(mode in ("boost", "search")),
                )
                found = _found(scenario["expected"], memories)
                results[mode] = {
                    "found": found,
                    "recall_at_5": 1.0 if found else 0.0,
                    "returned": len(memories),
                    "context_budget_share": _budget_share(app, memories),
                    "sibling_leak": _leaked(
                        scenario["siblings"], memories
                    ),
                    "contents": [
                        util.clip(str(item.get("content") or ""), 160)
                        for item in memories
                    ],
                }
            rows.append({
                "id": scenario["id"],
                "query": scenario["query"],
                "routed_domain": routed_name,
                "expected": list(scenario["expected"]),
                "siblings": list(scenario["siblings"]),
                "modes": results,
            })
        finally:
            try:
                app.shutdown()
            except Exception:
                pass
            shutil.rmtree(path, ignore_errors=True)

    summary: dict[str, Any] = {}
    for mode in modes:
        labeled = [row["modes"][mode] for row in rows]
        found = sum(1 for item in labeled if item["found"])
        leaks = sum(1 for item in labeled if item["sibling_leak"])
        shares = [float(item["context_budget_share"]) for item in labeled]
        summary[mode] = {
            "scenarios": len(labeled),
            "found": found,
            "recall_at_5": round(found / len(labeled), 4) if labeled else 0.0,
            "mean_context_budget_share": (
                round(sum(shares) / len(shares), 4) if shares else 0.0
            ),
            "sibling_leaks": leaks,
        }
    decision: dict[str, Any] | None = None
    if "boost" in summary:
        boost = summary["boost"]
        decision = {
            "mode": "boost",
            "recall_at_5": boost["recall_at_5"],
            "mean_context_budget_share": boost["mean_context_budget_share"],
            "sibling_leaks": boost["sibling_leaks"],
            "corpus_passes": (
                boost["recall_at_5"] == 1.0 and boost["sibling_leaks"] == 0
            ),
            "rationale": (
                "highest recall with no sibling leakage and a bounded "
                "context-budget share"
            ),
        }
    return {
        "experiment": tag,
        "corpus": _display_path(DEFAULT_CORPUS),
        "modes": list(modes),
        "top_k": TOP_K,
        "clip_chars": CLIP_CHARS,
        "default_decision": decision,
        "scenarios": rows,
        "summary": summary,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    modes = payload["modes"]
    lines = [
        f"# Cross-domain memory recall: {payload.get('experiment') or 'memory_recall'}",
        "",
        f"- Corpus: `{payload['corpus']}`",
        f"- Modes: {', '.join(modes)}",
        f"- Recall window: top {payload['top_k']}",
        f"- Budget share denominator: `context.section_budgets.memories` x 4",
    ]
    decision = payload.get("default_decision")
    if isinstance(decision, dict):
        result = (
            "passes" if decision.get("corpus_passes") else "does not fully pass"
        )
        lines.extend([
            f"- Selected default: `{decision.get('mode')}`",
            f"- Default corpus result: {result} "
            f"(recall@5 {float(decision.get('recall_at_5') or 0.0):.4f}, "
            f"mean budget share "
            f"{float(decision.get('mean_context_budget_share') or 0.0):.4f}, "
            f"sibling leaks {int(decision.get('sibling_leaks') or 0)})",
            "",
        ])
    if not isinstance(decision, dict):
        lines.append("")
    lines.extend([
        "## Summary",
        "",
        "| mode | recall@5 | found | mean context budget share | sibling leaks |",
        "| --- | ---: | ---: | ---: | ---: |",
    ])
    for mode in modes:
        item = payload["summary"][mode]
        lines.append(
            f"| {mode} | {item['recall_at_5']:.4f} "
            f"| {item['found']}/{item['scenarios']} "
            f"| {item['mean_context_budget_share']:.4f} "
            f"| {item['sibling_leaks']} |"
        )
    lines.extend([
        "",
        "## Scenarios",
        "",
        "| scenario | mode | routed domain | expected found | budget share | sibling leak |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ])
    for row in payload["scenarios"]:
        for mode in modes:
            item = row["modes"][mode]
            lines.append(
                f"| `{row['id']}` | {mode} | {row['routed_domain']} "
                f"| {'yes' if item['found'] else 'no'} "
                f"| {item['context_budget_share']:.4f} "
                f"| {'yes' if item['sibling_leak'] else 'no'} |"
            )
    lines.append("")
    return "\n".join(lines)


def _safe_tag(tag: str) -> str:
    return "".join(
        char if char.isalnum() or char in "._-" else "_" for char in tag
    ) or "memory_recall"


def write_reports(
    payload: dict[str, Any], out: Path, tag: str
) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    name = _safe_tag(tag)
    json_path = out / f"{name}.json"
    md_path = out / f"{name}.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_markdown(payload) + "\n", encoding="utf-8")
    return json_path, md_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--tag", default=None)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    corpus = Path(args.corpus)
    if not corpus.is_absolute():
        corpus = (Path.cwd() / corpus).resolve()
    modes = _modes(args.modes)
    tag = args.tag or (
        f"memory_recall_{modes[0]}" if len(modes) == 1 else "memory_recall_combined"
    )
    payload = measure(load_corpus(corpus), modes, tag=tag)
    payload["corpus"] = _display_path(corpus)
    json_path, md_path = write_reports(payload, Path(args.out), tag)
    parts = [
        f"{mode}: recall@5 {payload['summary'][mode]['recall_at_5']:.4f}"
        for mode in modes
    ]
    print(f"memory recall ({len(payload['scenarios'])} scenarios) - "
          + "; ".join(parts))
    print(f"wrote {json_path} and {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

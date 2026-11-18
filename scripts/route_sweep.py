"""Sweep the deterministic domain router over the routing corpus.

This tool builds a temporary App with the corpus fixture domains and asks
``domains.route`` for every message at a grid of thresholds and ambiguity
margins. Phase 10A recorded the baseline; Phase 10B reruns the same sweep
after deterministic evidence v2 to retune the shipped defaults.

    python scripts/route_sweep.py --tag phase10b_routing

Do not regenerate the committed phase10a files with this script: they record
the pre-evidence-v2 baseline and are intentionally historical.

The JSON and Markdown reports contain no timestamps or machine paths, so two
runs against the same corpus are byte-identical.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
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

from harness.app import build_app  # noqa: E402
from harness.evaluation.fixtures import apply_fixtures  # noqa: E402
from harness.evaluation.routing import (  # noqa: E402
    best_grid_point,
    routing_metrics,
)
from harness.model import ScriptedModel  # noqa: E402

DEFAULT_CORPUS = ROOT / "eval" / "scenarios" / "k_routing.yaml"
DEFAULT_OUT = ROOT / "eval" / "results"
DEFAULT_THRESHOLDS = "0.5,1.0,1.5,2.0,2.5,3.0"
DEFAULT_MARGINS = "0.5,0.75,1.0"


def _number_list(value: str) -> list[float]:
    numbers = [
        float(part.strip()) for part in str(value).split(",") if part.strip()
    ]
    if not numbers:
        raise argparse.ArgumentTypeError("expected at least one number")
    return numbers


def _display_path(path: Path) -> str:
    """A stable, machine-independent label for the report."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def load_corpus(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the raw YAML corpus, retaining each scenario's own fixtures."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    scenarios = data.get("scenarios") if isinstance(data, dict) else data
    if not isinstance(scenarios, list) or not scenarios:
        raise SystemExit(f"{path}: no scenarios found")
    cases: list[dict[str, Any]] = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        steps = scenario.get("steps") or []
        if len(steps) != 1 or not isinstance(steps[0], dict):
            raise SystemExit(
                f"{scenario.get('id')}: routing cases need exactly one turn"
            )
        routing = (steps[0].get("expect") or {}).get("routing") or {}
        expected = routing.get("domain")
        text = str(steps[0].get("input") or "")
        if not expected or not text:
            raise SystemExit(
                f"{scenario.get('id')}: missing expect.routing.domain or input"
            )
        fixtures = copy.deepcopy(scenario.get("fixtures") or {})
        cases.append({
            "id": str(scenario.get("id") or ""),
            "text": text,
            "expected": str(expected),
            "fixtures": fixtures,
        })
    fixtures = scenarios[0].get("fixtures") or {}
    domains = fixtures.get("domains") or []
    if not isinstance(domains, list) or not domains:
        raise SystemExit(f"{path}: first scenario has no fixture domains")
    return cases, domains


def _fixture_fingerprint(fixtures: dict[str, Any]) -> str:
    """Return a stable identity for one scenario's fixture state."""
    payload = json.dumps(
        fixtures,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sweep(
    cases: Sequence[dict[str, Any]],
    domains: Sequence[dict[str, Any]],
    thresholds: Sequence[float],
    margins: Sequence[float],
    *,
    data_dir: Path | None = None,
    tag: str = "route_sweep",
) -> dict[str, Any]:
    """Run every grid point against scenario-specific fixture state."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    case_fingerprints: list[str] = []
    for case in cases:
        fixtures = case.get("fixtures") or {"domains": list(domains)}
        fingerprint = _fixture_fingerprint(fixtures)
        grouped.setdefault(fingerprint, []).append(case)
        case_fingerprints.append(fingerprint)

    root = Path(data_dir) if data_dir is not None else None
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
    apps: dict[str, Any] = {}
    paths: dict[str, Path] = {}
    control_app: Any = None
    control_path: Path | None = None
    try:
        # One App represents one distinct fixture state. This keeps learned
        # memories, tasks, skills, and messages isolated exactly as they are
        # in scenario evaluation, while sharing the App within a group.
        for index, fingerprint in enumerate(sorted(grouped)):
            group_path = Path(tempfile.mkdtemp(
                prefix=f"route_sweep_{index}_",
                dir=str(root) if root is not None else None,
            ))
            app = build_app(group_path, model=ScriptedModel())
            fixtures = grouped[fingerprint][0].get("fixtures") or {
                "domains": list(domains)
            }
            apply_fixtures(app, copy.deepcopy(fixtures))
            app.config.set("domains.model_routing", "off")
            apps[fingerprint] = app
            paths[fingerprint] = group_path

        # General-only control: no specific domains exist, so every message
        # can only fall back to General. This is the Phase 10A delta baseline.
        control_path = Path(tempfile.mkdtemp(
            prefix="route_sweep_control_",
            dir=str(root) if root is not None else None,
        ))
        control_app = build_app(control_path, model=ScriptedModel())
        control_app.config.set("domains.model_routing", "off")

        grid: list[dict[str, Any]] = []
        for threshold in thresholds:
            for margin in margins:
                for app in apps.values():
                    app.config.set(
                        "domains.routing_threshold", float(threshold)
                    )
                    app.config.set("domains.ambiguity_margin", float(margin))
                expected: list[str] = []
                predicted: list[str] = []
                for case, fingerprint in zip(cases, case_fingerprints):
                    app = apps[fingerprint]
                    routing = app.runtime.preview_route(case["text"])
                    domain_id = str(routing.get("domain_id") or "general")
                    record = app.domains.get(domain_id)
                    expected.append(case["expected"])
                    predicted.append(
                        str(record["name"]) if record else domain_id
                    )
                metrics = routing_metrics(expected, predicted)
                grid.append({
                    "threshold": float(threshold),
                    "margin": float(margin),
                    **metrics,
                })

        control_expected = [case["expected"] for case in cases]
        control_predicted: list[str] = []
        for case in cases:
            routing = control_app.runtime.preview_route(case["text"])
            domain = control_app.domains.get(
                str(routing.get("domain_id") or "general")
            )
            control_predicted.append(
                str(domain["name"]) if domain else "General"
            )
        control = routing_metrics(control_expected, control_predicted)
    finally:
        for app in apps.values():
            try:
                app.shutdown()
            except Exception:
                pass
        for path in paths.values():
            shutil.rmtree(path, ignore_errors=True)
        if control_app is not None:
            try:
                control_app.shutdown()
            except Exception:
                pass
        if control_path is not None:
            shutil.rmtree(control_path, ignore_errors=True)

    best = best_grid_point(grid)
    return {
        "experiment": tag,
        "corpus": _display_path(DEFAULT_CORPUS),
        "case_count": len(cases),
        "thresholds": [float(item) for item in thresholds],
        "margins": [float(item) for item in margins],
        "grid": grid,
        "control": {
            "accuracy": control["accuracy"],
            "correct": control["correct"],
            "total": control["total"],
            "general_fallback_rate": control["general_fallback_rate"],
            "false_positive_rate": control["false_positive_rate"],
            "confusion": control["confusion"],
        },
        "best": {
            "threshold": best["threshold"],
            "margin": best["margin"],
            "accuracy": best["accuracy"],
            "general_fallback_rate": best["general_fallback_rate"],
            "false_positive_rate": best["false_positive_rate"],
            "correct": best["correct"],
            "total": best["total"],
            "confusion": best["confusion"],
        },
    }


def render_markdown(payload: dict[str, Any]) -> str:
    best = payload["best"]
    lines = [
        f"# Routing sweep: {payload.get('experiment') or 'route_sweep'}",
        "",
        f"- Corpus: `{payload['corpus']}`",
        f"- Cases: {payload['case_count']}",
        f"- Thresholds: {', '.join(str(item) for item in payload['thresholds'])}",
        f"- Margins: {', '.join(str(item) for item in payload['margins'])}",
        f"- Best: threshold {best['threshold']}, margin {best['margin']}",
        f"- Accuracy: {best['accuracy']:.4f} "
        f"({best['correct']}/{best['total']})",
        f"- General fallback rate: {best['general_fallback_rate']:.4f}",
        f"- False-positive rate: {best['false_positive_rate']:.4f}",
        f"- General-only control accuracy: "
        f"{payload['control']['accuracy']:.4f} "
        f"({payload['control']['correct']}/{payload['control']['total']})",
        "",
        "Tie-break: higher threshold, then lower margin.",
        "",
        "## Grid",
        "",
        "| threshold | margin | accuracy | general fallback | false positive | correct |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for point in payload["grid"]:
        lines.append(
            f"| {point['threshold']} | {point['margin']} "
            f"| {point['accuracy']:.4f} "
            f"| {point['general_fallback_rate']:.4f} "
            f"| {point['false_positive_rate']:.4f} "
            f"| {point['correct']}/{point['total']} |"
        )
    table = best["confusion"]
    labels = table["labels"]
    lines.extend([
        "",
        "## Confusion at the best point",
        "",
        "| expected \\ predicted | "
        + " | ".join(labels)
        + " | total |",
        "| --- | " + " | ".join("---:" for _ in labels) + " | ---: |",
    ])
    for row_label in labels:
        row = table["rows"][row_label]
        total = sum(row.values())
        lines.append(
            f"| {row_label} | "
            + " | ".join(str(row[column]) for column in labels)
            + f" | {total} |"
        )
    lines.append("")
    return "\n".join(lines)


def _safe_tag(tag: str) -> str:
    return "".join(
        char if char.isalnum() or char in "._-" else "_" for char in tag
    ) or "route_sweep"


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
    parser.add_argument("--thresholds", default=DEFAULT_THRESHOLDS)
    parser.add_argument("--margins", default=DEFAULT_MARGINS)
    parser.add_argument("--tag", default="route_sweep")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--data-dir", default=None,
        help="optional existing data directory; a temp dir is used otherwise",
    )
    args = parser.parse_args(argv)

    corpus = Path(args.corpus)
    if not corpus.is_absolute():
        corpus = (Path.cwd() / corpus).resolve()
    cases, domains = load_corpus(corpus)
    payload = sweep(
        cases,
        domains,
        _number_list(args.thresholds),
        _number_list(args.margins),
        data_dir=Path(args.data_dir) if args.data_dir else None,
        tag=args.tag,
    )
    payload["corpus"] = _display_path(corpus)
    json_path, md_path = write_reports(payload, Path(args.out), args.tag)
    best = payload["best"]
    print(
        f"routing sweep: {payload['case_count']} cases; best threshold "
        f"{best['threshold']} margin {best['margin']}: accuracy "
        f"{best['accuracy']:.4f}, general fallback "
        f"{best['general_fallback_rate']:.4f}, false positive "
        f"{best['false_positive_rate']:.4f}"
    )
    print(f"wrote {json_path} and {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

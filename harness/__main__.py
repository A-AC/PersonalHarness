from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from harness.app import build_harness
from harness.contracts.maintenance import ResetReport
from harness.measure import learning_report


def _data_dir(args: argparse.Namespace) -> Path:
    return Path(args.data_dir or os.environ.get("HARNESS_DATA_DIR") or "./data")


def format_reset_report(report: ResetReport) -> str:
    lines = [f"Reset scope: {report.scope}"]
    if report.removed:
        lines.append("Removed:")
        for item in report.removed:
            lines.append(f"  {item.path}  ({item.files} files, {item.bytes} bytes)")
    else:
        lines.append("Removed: nothing (already empty)")
    lines.append(f"Backup: {report.backup_path or 'disabled'}")
    lines.append(f"Seeded: {', '.join(report.seeded) if report.seeded else 'nothing'}")
    if report.cleared_references:
        lines.append(f"Cleared stale references: {report.cleared_references}")
    lines.append(f"Duration: {report.duration_ms} ms")
    return "\n".join(lines)


def _report_path(value: str, results_dir: Path) -> Path:
    path = Path(value)
    if path.exists():
        return path
    candidate = results_dir / f"{value}.json"
    return candidate if candidate.exists() else path


def _print_report_summary(path: Path) -> int:
    from harness.evaluation.compare import load_report

    try:
        document = load_report(path)
    except (OSError, ValueError) as exc:
        print(f"cannot read report {path}: {exc}")
        return 2
    summary = document.get("summary") or {}
    print(f"Report: {path}")
    print(f"Run: {document.get('run_id')} ({document.get('mode')}) tag={document.get('tag')}")
    for key in (
        "scenario_runs",
        "passed",
        "failed",
        "inconclusive",
        "skipped",
        "assertions_total",
        "assertions_passed",
        "critical_failures",
        "unauthorized_dispatch",
    ):
        print(f"{key}: {summary.get(key)}")
    for field in (
        "scenario_pass_rate",
        "assertion_pass_rate",
        "grounding_accuracy",
        "memory_scope_accuracy",
        "project_association_accuracy",
        "context_retrieval_success_rate",
        "decision_interpretation_accuracy",
        "unsupported_claim_rate",
        "incorrect_approval_rate",
    ):
        value = summary.get(field) or {}
        if value.get("rate") is None:
            print(f"{field}: n/a (n=0)")
        else:
            print(
                f"{field}: {value['rate']:.1%} "
                f"({value.get('numerator', 0)}/{value.get('denominator', 0)})"
            )
    latency = summary.get("latency") or {}
    print(
        "latency p50/p95/max: "
        f"{latency.get('p50_ms')}/{latency.get('p95_ms')}/{latency.get('max_ms')} ms "
        f"(n={latency.get('samples', 0)})"
    )
    return 0


def _run_scenario_eval(args: argparse.Namespace) -> int:
    from harness.evaluation.compare import compare_reports, render_comparison
    from harness.evaluation.loader import (
        ScenarioError,
        load_default_scenarios,
        select_scenarios,
    )
    from harness.evaluation.metrics import summarize
    from harness.evaluation.runner import (
        EndpointUnavailable,
        EvaluationError,
        render_terminal_summary,
        run_evaluation,
    )
    from harness.evaluation.safety import UnsafeDataDirectory

    results_dir = Path(args.results_dir) if args.results_dir else _data_dir(args) / "eval" / "results"
    if args.summary:
        return _print_report_summary(_report_path(args.summary, results_dir))
    if args.compare:
        left = _report_path(args.compare[0], results_dir)
        right = _report_path(args.compare[1], results_dir)
        try:
            print(render_comparison(compare_reports(left, right)))
        except (OSError, ValueError) as exc:
            print(f"cannot compare reports: {exc}")
            return 2
        return 0
    try:
        scenarios = load_default_scenarios(args.scenario_dir)
    except (OSError, ScenarioError) as exc:
        print(str(exc))
        return 2
    if args.list_scenarios:
        for item in scenarios:
            print(
                f"{item.category:<14}{item.severity:<9}{item.id:<42}"
                f"{','.join(item.modes):<15}{len(item.steps)} step(s)"
            )
            print(f"  {item.name}")
        return 0
    if args.mode is None:
        print("--mode is required to run scenarios (deterministic or live)")
        return 2
    try:
        run = run_evaluation(
            mode=args.mode,
            scenarios=scenarios,
            scenario_ids=args.scenario or args.only,
            categories=args.category,
            tags=args.scenario_tag,
            repeats=1 if args.repeats is None else args.repeats,
            tag=args.tag,
            sandbox_dir=args.sandbox,
            config_from=args.config_from,
            scenario_dir=args.scenario_dir,
            results_dir=results_dir,
        )
    except (UnsafeDataDirectory, EndpointUnavailable, EvaluationError, ScenarioError, ValueError) as exc:
        print(str(exc))
        return 2
    summary = summarize(run)
    if run.results_dir:
        print(render_terminal_summary(run, summary))
        print(f"Reports: {results_dir / (run.tag + '.json')} and {results_dir / (run.tag + '.md')}")
    else:
        print(render_terminal_summary(run, summary))
    failures = summary["failed"] + summary["inconclusive"]
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness")
    parser.add_argument("--data-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve")
    sub.add_parser("health")
    sub.add_parser("consolidate")
    sub.add_parser("review")
    sub.add_parser("skills")
    sub.add_parser("tick")
    sub.add_parser("status")
    sub.add_parser("measure")
    reset = sub.add_parser("reset")
    reset.add_argument(
        "--scope",
        choices=("full", "memories", "conversations"),
        default="full",
        help="full (default), memories only, or conversations/traces only",
    )
    reset.add_argument("--yes", action="store_true", help="required confirmation for the CLI")
    reset.add_argument("--no-backup", action="store_true", help="skip the timestamped archive")
    turn = sub.add_parser("turn")
    turn.add_argument("text")
    evaluation = sub.add_parser("eval")
    evaluation.add_argument(
        "--mode",
        choices=("deterministic", "live"),
        default=None,
        help="run the scenario suite (omit for the legacy harness.eval suite)",
    )
    evaluation.add_argument("--repeats", type=int, default=None)
    evaluation.add_argument("--tag", default=None, help="run tag / report filename")
    evaluation.add_argument("--only", default=None, help="comma-separated scenario id or category fragments")
    evaluation.add_argument("--scenario", default=None, help="comma-separated scenario ID fragments")
    evaluation.add_argument("--category", default=None, help="comma-separated category fragments")
    evaluation.add_argument("--scenario-tag", default=None, help="comma-separated scenario tags")
    evaluation.add_argument("--scenario-dir", default=None, help="YAML/JSON scenario directory or file")
    evaluation.add_argument("--sandbox", default=None, help="dedicated evaluation data directory")
    evaluation.add_argument("--config-from", default=None, help="read deployed settings from this data dir")
    evaluation.add_argument("--list", action="store_true", dest="list_scenarios")
    evaluation.add_argument("--results-dir", default=None)
    evaluation.add_argument("--summary", default=None, metavar="REPORT_JSON")
    evaluation.add_argument("--compare", nargs=2, metavar=("TAG_A", "TAG_B"))
    args = parser.parse_args(argv)
    if args.command == "eval":
        if args.mode is not None or args.scenario_dir or args.summary:
            return _run_scenario_eval(args)
        from harness.eval import SCENARIOS, compare_results, run_suite

        if args.list_scenarios:
            for item in SCENARIOS:
                turns = len(item.turns)
                print(f"{item.category:<16}{item.id}  ({turns} turn{'s' if turns > 1 else ''})")
            return 0
        results_dir = Path(args.results_dir) if args.results_dir else _data_dir(args) / "eval" / "results"
        if args.compare:
            compare_results(results_dir / f"{args.compare[0]}.json", results_dir / f"{args.compare[1]}.json")
            return 0
        try:
            run_suite(
                data_dir=_data_dir(args),
                repeats=3 if args.repeats is None else args.repeats,
                only=args.only,
                tag=args.tag or "run",
                results_dir=results_dir,
            )
        except ValueError as exc:
            print(str(exc))
            return 2
        return 0
    harness = build_harness(_data_dir(args))
    if args.command == "health":
        print(json.dumps({"status": "ok", "data_dir": str(harness.data_dir)}))
        return 0
    if args.command == "reset":
        if not args.yes:
            print("reset not run: pass --yes to confirm")
            return 2
        report = harness.reset.run(
            args.scope,
            actor="cli",
            backup=False if args.no_backup else None,
        )
        print(format_reset_report(report))
        return 0
    if args.command == "turn":
        result = harness.chat.submit(args.text)
        print("\n".join(result.messages))
        if result.error:
            print(result.error)
            return 1
        return 0
    if args.command == "consolidate":
        run = harness.consolidation.run(trigger="cli")
        print(json.dumps({"id": run.id, "status": run.status, "results": run.results, "findings": run.findings}, default=str))
        return 0 if run.status == "completed" else 1
    if args.command == "review":
        report = harness.learning_review.review(trigger="cli")
        print(
            json.dumps(
                {
                    "ts": report.ts,
                    "trigger": report.trigger,
                    "changed": report.changed,
                    "candidates": report.candidates,
                    "items": [
                        {"kind": item.kind, "action": item.action, "detail": item.detail}
                        for item in report.items
                    ],
                    "notes": report.notes,
                },
                default=str,
            )
        )
        return 0
    if args.command == "skills":
        rows = [
            {
                "id": record.id,
                "name": record.name,
                "status": record.status,
                "preconditions": record.preconditions,
                "tested": bool((record.test_evidence or {}).get("passed")),
                "supersedes": record.supersedes,
                "reason": record.provenance.get("reason") or record.provenance.get("source_kind"),
                "updated_at": record.updated_at,
            }
            for record in harness.skill_library.list()
        ]
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if args.command == "tick":
        report = harness.scheduler.tick()
        print(
            json.dumps(
                {
                    "ts": report.ts,
                    "enabled": report.enabled,
                    "summary": report.summary,
                    "ran": report.ran,
                    "model_used": report.model_used,
                    "items": [
                        {
                            "kind": item.kind,
                            "action": item.action,
                            "task_id": item.task_id,
                            "reason": item.reason,
                        }
                        for item in report.items
                    ],
                },
                default=str,
            )
        )
        return 0
    if args.command == "status":
        work = harness.scheduler.report()
        print(work.summary())
        return 0
    if args.command == "measure":
        print(json.dumps(learning_report(harness), indent=2))
        return 0
    if args.command == "serve":
        import uvicorn

        from harness.ui.web import create_app

        host = str(harness.settings.get("ui.bind_host"))
        port = int(harness.settings.get("ui.bind_port"))
        uvicorn.run(create_app(harness), host=host, port=port, log_config=None)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

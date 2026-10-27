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
    evaluation.add_argument("--repeats", type=int, default=3)
    evaluation.add_argument("--tag", default="run")
    evaluation.add_argument("--only", default=None, help="comma-separated scenario id or category fragments")
    evaluation.add_argument("--list", action="store_true", dest="list_scenarios")
    evaluation.add_argument("--results-dir", default=None)
    evaluation.add_argument("--compare", nargs=2, metavar=("TAG_A", "TAG_B"))
    args = parser.parse_args(argv)
    if args.command == "eval":
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
                repeats=args.repeats,
                only=args.only,
                tag=args.tag,
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

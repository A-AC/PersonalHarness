from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from harness.app import build_harness
from harness.measure import learning_report


def _data_dir(args: argparse.Namespace) -> Path:
    return Path(args.data_dir or os.environ.get("HARNESS_DATA_DIR") or "./data")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness")
    parser.add_argument("--data-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve")
    sub.add_parser("health")
    sub.add_parser("consolidate")
    sub.add_parser("measure")
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
    if args.command == "turn":
        result = harness.loop.handle_turn(args.text)
        print("\n".join(result.messages))
        if result.error:
            print(result.error)
            return 1
        return 0
    if args.command == "consolidate":
        run = harness.consolidation.run(trigger="cli")
        print(json.dumps({"id": run.id, "status": run.status, "results": run.results, "findings": run.findings}, default=str))
        return 0 if run.status == "completed" else 1
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

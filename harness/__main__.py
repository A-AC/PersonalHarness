"""Terminal interface: a client of the running application.

The CLI never launches its own model loop and never holds the state store
open for writing: every command is an HTTP call to the one server process
(``harness serve``), so the web UI and the terminal always share the same
runtime, history, and state. Diagnostics that must run before the server
exists (``endpoint-check``) read the config file directly but change nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__


def _data_dir_default() -> Path:
    return Path(os.environ.get("HARNESS_DATA_DIR", "data"))


def _load_token(data_dir: Path) -> str:
    try:
        import yaml

        path = data_dir / "config.yaml"
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return str((data.get("server") or {}).get("token", ""))
    except Exception:
        pass
    return ""


def _base_url(args: argparse.Namespace) -> str:
    url = args.url or os.environ.get("HARNESS_URL", "")
    if not url:
        host = os.environ.get("HARNESS_HOST", "127.0.0.1")
        port = os.environ.get("HARNESS_PORT", "8080")
        url = f"http://{host}:{int(port)}"
    return url.rstrip("/")


def _token(args: argparse.Namespace) -> str:
    token = args.token or os.environ.get("HARNESS_TOKEN", "")
    if not token:
        token = _load_token(Path(args.data_dir))
    return token


class Client:
    def __init__(self, base: str, token: str):
        self.base = base
        self.token = token

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise SystemExit(f"error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SystemExit(
                f"cannot reach the harness server at {self.base} "
                f"({exc.reason}). Start it with: python -m harness serve"
            ) from exc


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> None:
    from .app import App
    from .config import load_config
    from .server import serve

    data_dir = Path(args.data_dir)
    config = load_config(data_dir)
    app = App(data_dir, config=config)
    serve(app, host=args.host, port=args.port)


def cmd_endpoint_check(args: argparse.Namespace) -> None:
    """Direct diagnostic: verifies the configured endpoint without a server."""
    from .config import load_config
    from .model import ModelClient

    config = load_config(Path(args.data_dir))
    client = ModelClient(config)
    try:
        result = client.probe()
    finally:
        client.close()
    print(json.dumps(result, indent=2, default=str))
    ok = result.get("reachable") and result.get("chat_ok")
    raise SystemExit(0 if ok else 1)


def cmd_message(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    out = client.call("POST", "/api/message", {"text": args.text, "source": "cli"})
    if out.get("message"):
        print(out["message"])
    print(f"[{out.get('status')}] task={out.get('task_id')} run={out.get('run_id')}")


def cmd_status(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    state = client.call("GET", "/api/state")
    print("== open work ==")
    for task in state.get("open_tasks", []):
        print(
            f"  [{task['status']:>16}] {task['objective'][:70]}"
            f"  ({task['status_reason'] or '-'})"
        )
    if not state.get("open_tasks"):
        print("  (none)")
    print("== pending approvals ==")
    for approval in state.get("approvals", []):
        print(f"  {approval['id']}  {approval['tool']}  {approval['arguments']}")
    if not state.get("approvals"):
        print("  (none)")
    print("== schedules ==")
    for schedule in state.get("schedules", []):
        print(
            f"  {'on ' if schedule['enabled'] else 'off'} "
            f"{schedule['kind']:<8} {schedule['name'][:30]:<32}"
            f" next={schedule['next_due_at'] or '-'} tz={schedule['tz']}"
        )


def cmd_tasks(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    if args.task_id:
        detail = client.call("GET", f"/api/tasks/{args.task_id}")
        print(json.dumps(detail, indent=2, default=str)[:4000])
        return
    state = client.call("GET", "/api/state")
    for task in state.get("tasks", []):
        print(f"[{task['status']:>16}] {task['id']}  {task['objective'][:70]}")


def cmd_approvals(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    state = client.call("GET", "/api/state")
    pending = state.get("approvals", [])
    if args.decision:
        if not pending and not args.approval_id:
            raise SystemExit("no pending approvals")
        approval_id = args.approval_id or pending[0]["id"]
        if args.decision == "cancel":
            client.call(
                "POST", f"/api/approvals/{approval_id}/cancel", {}
            )
            print(f"cancelled {approval_id}")
            return
        out = client.call(
            "POST", f"/api/approvals/{approval_id}/{args.decision}", {}
        )
        print(f"{args.decision}: {out}")
        return
    for approval in pending:
        print(f"{approval['id']}  [{approval['risk']}] {approval['tool']}")
        print(f"    arguments: {json.dumps(approval['arguments'])}")
        print(f"    reason: {approval['reason']}")
    if not pending:
        print("(no pending approvals)")


def cmd_schedules(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    if args.add:
        try:
            body = json.loads(args.add)
        except ValueError as exc:
            raise SystemExit(f"--add expects a JSON object: {exc}") from exc
        if not isinstance(body, dict):
            raise SystemExit("--add expects a JSON object")
        out = client.call("POST", "/api/schedules", body)
        print(json.dumps(out, indent=2, default=str))
        return
    if args.action:
        schedule_id, action = args.action
        out = client.call(
            "POST", f"/api/schedules/{schedule_id}/{action}", {}
        )
        print(json.dumps(out, indent=2, default=str))
        return
    out = client.call("GET", "/api/schedules")
    for schedule in out.get("schedules", []):
        print(
            f"{schedule['id']}  {'on ' if schedule['enabled'] else 'off'} "
            f"{schedule['kind']:<8} {schedule['name']}"
        )
        print(f"    prompt: {schedule['prompt'][:90]}")
        print(
            f"    next={schedule['next_due_at'] or '-'} tz={schedule['tz']}"
            f" at={schedule['at_ts'] or schedule['time_of_day'] or schedule['interval_s']}"
        )


def cmd_memories(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    if args.search:
        out = client.call("POST", "/api/memories/search", {"query": args.search})
        for item in out.get("results", []):
            print(f"[{item['kind']}] ({item['scope']}) {item['content']}")
            print(f"    id={item['id']} score={item.get('score')}")
        for item in out.get("history", []):
            print(f"[history {item['ts'][:10]}] {item['content'][:100]}")
        return
    out = client.call("GET", "/api/memories")
    for item in out.get("memories", []):
        print(f"[{item['kind']}] ({item['scope']}) rev{item['revision']} {item['content'][:100]}")
        print(f"    id={item['id']} updated={item['updated_at']}")
    problems = out.get("artifact_problems", [])
    if problems:
        print(f"artifact problems: {json.dumps(problems)}")


def cmd_domains(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    if args.create:
        out = client.call("POST", "/api/domains", {
            "name": args.create[0], "description": args.create[1] or "",
        })
        print(json.dumps(out, indent=2, default=str))
        return
    out = client.call("GET", "/api/domains")
    for domain in out.get("domains", []):
        print(f"[{domain['status']}] {domain['name']} ({domain['id']})")
        if domain["description"]:
            print(f"    {domain['description']}")


def cmd_consolidate(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    out = client.call("POST", "/api/consolidate", {})
    print(f"consolidation {out.get('status')}: "
          f"applied={len(out.get('applied', []))} "
          f"rejected={len(out.get('rejected', []))} "
          f"log={out.get('log_path')}")


def cmd_events(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    out = client.call("GET", f"/api/events?limit={args.limit}")
    for event in out.get("events", []):
        data = event.get("data")
        data_text = json.dumps(data)[:140] if data else ""
        print(f"{event['ts']} [{event.get('level', 'info')}] "
              f"{event['type']} {data_text}")


def cmd_tools(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    out = client.call("GET", "/api/tools")
    for tool in out.get("tools", []):
        print(f"[{tool['risk']:>7}] {tool['name']}")
    if out.get("mcp_status"):
        print("mcp: " + json.dumps(out["mcp_status"]))


def cmd_backup(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    out = client.call("POST", "/api/backup", {})
    print(f"backup written: {out.get('backup')}")


def cmd_restore(args: argparse.Namespace) -> None:
    if not args.yes:
        raise SystemExit("restore is destructive; pass --yes to confirm")
    client = Client(_base_url(args), _token(args))
    out = client.call("POST", "/api/restore", {"path": args.path, "confirm": True})
    print(json.dumps(out, indent=2))
    print("The service is restarting itself; it will be back in a few seconds.")


def cmd_config(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    out = client.call("GET", "/api/config")
    print(json.dumps(out.get("config", {}), indent=2, default=str))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Personal assistant harness (terminal client + server).",
    )
    parser.add_argument("--version", action="version",
                        version=f"harness {__version__}")
    # Global connection options (usable before or after the subcommand).
    parser.add_argument("--data-dir", default=None,
                        help="data directory (config/token lookup; serve uses "
                             "it for state)")
    parser.add_argument("--url", default=None,
                        help="harness server URL (default HARNESS_URL or "
                             "http://host:port)")
    parser.add_argument("--token", default=None,
                        help="access token (default HARNESS_TOKEN or the "
                             "token in <data-dir>/config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        # SUPPRESS keeps pre-subcommand values when the option is not
        # repeated after the subcommand.
        for flag in ("--data-dir", "--url", "--token"):
            p.add_argument(flag, default=argparse.SUPPRESS)

    serve = sub.add_parser("serve", help="run the application (web UI, API, "
                                         "scheduler, heartbeat)")
    common(serve)
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=cmd_serve)

    check = sub.add_parser("endpoint-check",
                           help="verify the configured model endpoint")
    common(check)
    check.set_defaults(func=cmd_endpoint_check)

    for name in ("msg", "turn", "say"):
        p = sub.add_parser(name, help="send a message" if name == "msg"
                           else "send a message (alias)")
        common(p)
        p.add_argument("text")
        p.set_defaults(func=cmd_message)

    p = sub.add_parser("status", help="open work, approvals, schedules")
    common(p); p.set_defaults(func=cmd_status)

    p = sub.add_parser("tasks", help="list tasks or show one task's detail")
    common(p)
    p.add_argument("task_id", nargs="?", default=None)
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("approvals", help="list pending approvals")
    common(p)
    p.add_argument("decision", choices=["approve", "reject", "cancel"],
                   nargs="?", default=None)
    p.add_argument("approval_id", nargs="?", default=None)
    p.set_defaults(func=cmd_approvals)

    p = sub.add_parser("schedules", help="list schedules")
    common(p)
    p.add_argument("--add", metavar="JSON", default=None,
                   help='add a schedule, e.g. '
                        '{"name":"daily","prompt":"...","kind":"daily",'
                        '"time_of_day":"09:00"}')
    p.add_argument("--action", nargs=2, metavar=("ID", "enable|disable|delete"))
    p.set_defaults(func=cmd_schedules)

    p = sub.add_parser("memories", help="list or search memories")
    common(p)
    p.add_argument("--search", default=None)
    p.set_defaults(func=cmd_memories)

    p = sub.add_parser("domains", help="list domains")
    common(p)
    p.add_argument("--create", nargs=2, metavar=("NAME", "DESCRIPTION"))
    p.set_defaults(func=cmd_domains)

    p = sub.add_parser("consolidate", help="run one consolidation pass")
    common(p); p.set_defaults(func=cmd_consolidate)

    p = sub.add_parser("events", help="show recent events")
    common(p)
    p.add_argument("-N", "--limit", type=int, default=100)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("tools", help="list registered tools")
    common(p); p.set_defaults(func=cmd_tools)

    p = sub.add_parser("backup", help="write a backup archive")
    common(p); p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="restore state from a backup")
    common(p)
    p.add_argument("path")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("config", help="show the effective configuration")
    common(p); p.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "data_dir", None):
        args.data_dir = str(_data_dir_default())
    args.func(args)


if __name__ == "__main__":
    main()

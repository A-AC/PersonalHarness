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
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, util


def _local_time(value: str | None) -> str:
    """Render a stored UTC timestamp in this computer's local time."""
    if not value:
        return "-"
    return util.parse_iso(value).astimezone(util.system_tz()).strftime(
        "%Y-%m-%d %H:%M"
    )


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
            f" next={_local_time(schedule['next_due_at'])} tz={schedule['tz']}"
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
            f"    next={_local_time(schedule['next_due_at'])}"
            f" tz={schedule['tz']}"
            f" at={schedule['at_ts'] or schedule['time_of_day'] or schedule['interval_s']}"
        )


def cmd_inbox(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    if args.add:
        try:
            body = json.loads(args.add)
        except ValueError as exc:
            raise SystemExit(f"--add expects a JSON object: {exc}") from exc
        if not isinstance(body, dict):
            raise SystemExit("--add expects a JSON object")
        out = client.call("POST", "/api/inbox", body)
        print(json.dumps(out, indent=2, default=str))
        return
    if args.ignore:
        out = client.call(
            "POST", f"/api/inbox/{urllib.parse.quote(args.ignore, safe='')}"
            "/ignore", {}
        )
        print(json.dumps(out, indent=2, default=str))
        return
    if args.triage:
        out = client.call("POST", "/api/inbox/triage", {})
        print(json.dumps(out, indent=2, default=str))
        return
    if args.status:
        out = client.call("GET", f"/api/inbox?status={args.status}")
    else:
        out = client.call("GET", "/api/inbox")
    for event in out.get("events", []):
        print(
            f"{event['id']}  [{event['status']}/{event['trust']}] "
            f"{event['source']}: {event['title'][:80]}"
        )
        print(
            f"    kind={event['kind']} severity={event['severity']} "
            f"received={_local_time(event['received_at'])}"
        )
        if event.get("body"):
            print(f"    {event['body'][:160]}")
    if not out.get("events"):
        print("(no inbox events)")
    print(f"automations={len(out.get('automations') or [])}")


def cmd_automations(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    if args.add:
        try:
            body = json.loads(args.add)
        except ValueError as exc:
            raise SystemExit(f"--add expects a JSON object: {exc}") from exc
        if not isinstance(body, dict):
            raise SystemExit("--add expects a JSON object")
        out = client.call("POST", "/api/automations", body)
        print(json.dumps(out, indent=2, default=str))
        return
    if args.update:
        automation_id, raw = args.update
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise SystemExit(f"--update expects a JSON object: {exc}") from exc
        if not isinstance(body, dict):
            raise SystemExit("--update expects a JSON object")
        out = client.call(
            "PATCH",
            f"/api/automations/{urllib.parse.quote(automation_id, safe='')}",
            body,
        )
        print(json.dumps(out, indent=2, default=str))
        return
    if args.action:
        automation_id, action = args.action
        out = client.call(
            "POST",
            f"/api/automations/{urllib.parse.quote(automation_id, safe='')}"
            f"/{action}", {},
        )
        print(json.dumps(out, indent=2, default=str))
        return
    out = client.call("GET", "/api/automations")
    for rule in out.get("automations", []):
        print(
            f"{rule['id']}  {'on ' if rule['enabled'] else 'off'} "
            f"[{rule['action']}] {rule['name']}"
        )
        keywords = ", ".join(rule.get("match_keywords") or []) or "-"
        print(
            f"    source={rule.get('source') or '*'} "
            f"kind={rule.get('kind') or '*'} keywords={keywords} "
            f"cooldown={rule.get('cooldown_s')} "
            f"max/hour={rule.get('max_per_hour')}"
        )
        if rule.get("disabled_reason"):
            print(f"    disabled: {rule['disabled_reason']}")
    if not out.get("automations"):
        print("(no automations)")
    fires = out.get("fires") or []
    for fire in fires[:20]:
        print(
            f"    fire {_local_time(fire['fired_at'])} "
            f"{fire.get('automation_name') or '(default)'} "
            f"{fire['status']} {fire.get('reason') or ''}"
        )


def cmd_memories(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    action_id = (
        args.archive or args.restore or args.pin or args.unpin
    )
    if action_id:
        action = (
            "archive" if args.archive else
            "restore" if args.restore else
            "pin" if args.pin else "unpin"
        )
        out = client.call(
            "POST", f"/api/memories/{urllib.parse.quote(action_id, safe='')}"
            f"/{action}", {}
        )
        print(json.dumps(out, indent=2, default=str))
        return
    if args.search:
        body = {"query": args.search}
        if args.include_archived:
            body["include_archived"] = True
        out = client.call("POST", "/api/memories/search", body)
        for item in out.get("results", []):
            label = " [archived]" if item.get("archived") else ""
            print(f"[{item['kind']}] ({item['scope']}){label} "
                  f"{item['content']}")
            print(
                f"    id={item['id']} score={item.get('score')} "
                f"uses={item.get('access_count', 0)}"
            )
        for item in out.get("history", []):
            print(f"[history {_local_time(item['ts'])[:10]}]"
                  f" {item['content'][:100]}")
        return
    status = "archived" if args.archived else ("any" if args.all else "active")
    out = client.call("GET", f"/api/memories?status={status}")
    for item in out.get("memories", []):
        flags = []
        if item.get("archived"):
            flags.append("archived")
        if item.get("pinned"):
            flags.append("pinned")
        suffix = f"  ({', '.join(flags)})" if flags else ""
        print(
            f"[{item['kind']}] ({item['scope']}) rev{item['revision']} "
            f"uses={item.get('access_count', 0)}{suffix} "
            f"{item['content'][:100]}"
        )
        print(
            f"    id={item['id']} updated={item['updated_at']}"
            + (f" last_used={item['last_accessed_at']}"
               if item.get("last_accessed_at") else "")
        )
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


def _module_action_path(module_id: str, action: str) -> str:
    """Quote a module id for a path segment (ids may contain spaces)."""
    quoted = urllib.parse.quote(module_id, safe=":")
    return f"/api/modules/{quoted}/{action}"


def cmd_skills(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    if args.activate:
        out = client.call(
            "POST", _module_action_path(args.activate, "activate"), {}
        )
        print(json.dumps(out, indent=2, default=str))
        return
    if args.disable:
        out = client.call(
            "POST", _module_action_path(args.disable, "disable"), {}
        )
        print(json.dumps(out, indent=2, default=str))
        return
    if args.rollback:
        out = client.call(
            "POST", _module_action_path(args.rollback, "rollback"), {}
        )
        print(json.dumps(out, indent=2, default=str))
        return
    if args.reload is not None:
        path = (
            _module_action_path(args.reload, "reload")
            if args.reload else "/api/modules/reload"
        )
        out = client.call("POST", path, {})
        print(json.dumps(out, indent=2, default=str))
        return
    out = client.call("GET", "/api/skills")
    for skill in out.get("skills", []):
        flags = []
        if skill.get("agent_owned"):
            flags.append("agent-owned")
        if skill.get("rollback_available"):
            flags.append("rollback")
        if skill.get("created_by"):
            flags.append(f"by={skill['created_by']}")
        suffix = f"  ({', '.join(flags)})" if flags else ""
        print(
            f"{skill['id']}  [{skill['status']}/{skill['scope']}] "
            f"v{skill['version']}  {skill['name']}{suffix}"
        )
        if skill.get("description"):
            print(f"    {skill['description']}")
        preconditions = ", ".join(skill.get("preconditions") or []) or "-"
        domains = ", ".join(skill.get("domains") or []) or "-"
        print(
            f"    preconditions={preconditions} domains={domains} "
            f"chars={skill.get('body_chars', 0)}"
        )
    if not out.get("skills"):
        print("(no skills)")
    print(
        f"enabled={out.get('enabled')} auto_draft={out.get('auto_draft')} "
        f"max_body_chars={out.get('max_body_chars')}"
    )


def cmd_modules(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    if args.reload is not None:
        path = (
            _module_action_path(args.reload, "reload")
            if args.reload else "/api/modules/reload"
        )
        out = client.call("POST", path, {})
        print(json.dumps(out, indent=2, default=str))
        return
    if args.enable:
        out = client.call("POST", _module_action_path(args.enable, "enable"), {})
        print(json.dumps(out, indent=2, default=str))
        return
    if args.disable:
        out = client.call(
            "POST", _module_action_path(args.disable, "disable"), {}
        )
        print(json.dumps(out, indent=2, default=str))
        return
    out = client.call("GET", "/api/modules")
    for module in out.get("modules", []):
        print(
            f"{module['id']}  [{module['kind']}/{module['scope']}] "
            f"{module['status']}  {module['name']}"
        )
        domains = ", ".join(module.get("bound_domains") or []) or "-"
        print(
            f"    domains={domains} tools="
            f"{len(module.get('tools') or [])} "
            f"health={module.get('health') or '-'}"
        )
        if module.get("last_error"):
            print(f"    last_error: {module['last_error']}")


def cmd_tools(args: argparse.Namespace) -> None:
    client = Client(_base_url(args), _token(args))
    out = client.call("GET", "/api/tools")
    for tool in out.get("tools", []):
        flags = []
        if tool.get("untrusted_output"):
            flags.append("untrusted")
        if tool.get("read_only"):
            flags.append("read-only")
        elif tool.get("side_effects"):
            flags.append("side-effects")
        suffix = f"  ({', '.join(flags)})" if flags else ""
        print(f"[{tool['risk']:>7}] {tool['name']}{suffix}")
    if out.get("mcp_status"):
        print("modules: " + json.dumps(out["mcp_status"]))


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


def cmd_eval(args: argparse.Namespace) -> None:
    """Run scenarios in-process; never touches the deployed data directory."""
    from .evaluation.cli import cmd_eval as run_eval

    run_eval(args)


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

    p = sub.add_parser("inbox", help="list, add, ignore, or triage inbox events")
    common(p)
    p.add_argument("--add", metavar="JSON", default=None,
                   help='push an event, e.g. {"source":"mail",'
                        '"external_id":"42","title":"..."}')
    p.add_argument("--ignore", metavar="ID", default=None)
    p.add_argument("--triage", action="store_true")
    p.add_argument("--status", choices=["new", "triaged", "promoted",
                                        "ignored", "deduped"], default=None)
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("automations", help="list or manage inbox automations")
    common(p)
    p.add_argument("--add", metavar="JSON", default=None,
                   help='create a rule, e.g. {"name":"mail",'
                        '"source":"mail","action":"notify"}')
    p.add_argument("--update", nargs=2, metavar=("ID", "JSON"),
                   help='patch fields, e.g. {"enabled":false}')
    p.add_argument("--action", nargs=2, metavar=("ID", "enable|disable|delete"))
    p.set_defaults(func=cmd_automations)

    p = sub.add_parser("memories", help="list, search, archive, or restore memories")
    common(p)
    p.add_argument("--search", default=None)
    p.add_argument("--archived", action="store_true",
                   help="list archived memories only")
    p.add_argument("--all", action="store_true",
                   help="list every status, including archived")
    p.add_argument("--include-archived", action="store_true",
                   help="include archived rows in a search (restores them)")
    p.add_argument("--archive", metavar="ID", default=None)
    p.add_argument("--restore", metavar="ID", default=None)
    p.add_argument("--pin", metavar="ID", default=None)
    p.add_argument("--unpin", metavar="ID", default=None)
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

    p = sub.add_parser("modules", help="list, reload, enable, or disable modules")
    common(p)
    p.add_argument("--reload", nargs="?", const="", metavar="ID", default=None,
                   help="reload one module, or every module when ID is omitted")
    p.add_argument("--enable", metavar="ID", default=None)
    p.add_argument("--disable", metavar="ID", default=None)
    p.set_defaults(func=cmd_modules)

    p = sub.add_parser("skills", help="list or manage skill drafts and versions")
    common(p)
    p.add_argument("--activate", metavar="ID", default=None)
    p.add_argument("--disable", metavar="ID", default=None)
    p.add_argument("--rollback", metavar="ID", default=None)
    p.add_argument("--reload", nargs="?", const="", metavar="ID", default=None,
                   help="reload one skill, or every module when ID is omitted")
    p.set_defaults(func=cmd_skills)

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

    p = sub.add_parser(
        "eval",
        help="run scenario evaluations (deterministic or against a live model)",
    )
    common(p)
    p.add_argument("--mode", choices=["deterministic", "live"],
                   default="deterministic")
    p.add_argument("--scenario-dir", default=None,
                   help="directory of YAML scenario files "
                        "(default eval/scenarios)")
    p.add_argument("--scenario", default=None,
                   help="comma-separated scenario ids or glob patterns")
    p.add_argument("--category", default=None)
    p.add_argument("--scenario-tag", default=None)
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--tag", default=None,
                   help="report name under the results directory")
    p.add_argument("--results-dir", default=None,
                   help="where JSON/Markdown reports are written "
                        "(default data/eval/results)")
    p.add_argument("--sandbox", default=None,
                   help="scratch root (default: the OS temp directory)")
    p.add_argument("--config-from", default=None,
                   help="data directory whose config.yaml supplies model "
                        "settings (default ./data)")
    p.add_argument("--model", default=None,
                   help="override model.name")
    p.add_argument("--base-url", default=None,
                   help="override model.base_url, e.g. http://host:8181")
    p.add_argument("--api-key", default=None,
                   help="override model.api_key")
    p.add_argument("--allow-unlisted-model", action="store_true",
                   help="run even when /v1/models does not advertise the "
                        "requested model")
    p.add_argument("--keep-sandbox", action="store_true")
    p.add_argument("--list", action="store_true",
                   help="list selected scenarios without running them")
    p.add_argument("--list-models", action="store_true",
                   help="probe the endpoint and print the models it serves")
    p.add_argument("--summary", default=None,
                   help="print the stored summary for a report tag or path")
    p.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                   help="compare two stored reports by tag or path")
    p.set_defaults(func=cmd_eval)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "data_dir", None):
        args.data_dir = str(_data_dir_default())
    args.func(args)


if __name__ == "__main__":
    main()

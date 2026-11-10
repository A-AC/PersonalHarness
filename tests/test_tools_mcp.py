"""Phase 1: registry-backed MCP modules, web search, and trust flags."""

from __future__ import annotations

import contextlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import util
from harness.model import ToolCall

REPO = Path(__file__).resolve().parents[1]


def _echo_server(tmp_path: Path) -> Path:
    server_file = tmp_path / "echo_server.py"
    server_file.write_text((REPO / "examples" / "mcp" / "echo_server.py")
                           .read_text(encoding="utf-8"), encoding="utf-8")
    return server_file


def _configure_mcp(
    app,
    tmp_path: Path,
    *,
    autoapprove: bool = False,
    read_only: bool = False,
    domains: list[str] | None = None,
    tools_allow: list[str] | None = None,
    tools_deny: list[str] | None = None,
    name: str = "test_echo",
) -> Path:
    """Write a legacy import file, then reload the registry."""
    import yaml

    entry: dict = {
        "name": name,
        "command": sys.executable,
        "args": [str(_echo_server(tmp_path))],
        "autoapprove": autoapprove,
    }
    if read_only:
        entry["read_only"] = True
    if domains:
        entry["domains"] = list(domains)
    if tools_allow:
        entry["tools_allow"] = list(tools_allow)
    if tools_deny:
        entry["tools_deny"] = list(tools_deny)
    config_path = tmp_path / "mcp.yaml"
    config_path.write_text(yaml.safe_dump({"servers": [entry]}),
                           encoding="utf-8")
    app.config.set("tools.mcp_servers_file", str(config_path))
    app.modules.reload()
    return config_path


def _call(name: str, arguments: dict) -> ToolCall:
    return ToolCall(id="call_1", name=name, arguments=arguments,
                    raw_arguments=json.dumps(arguments))


def _spec(app, name: str):
    return next(spec for spec in app.tools.specs() if spec.name == name)


# ---------------------------------------------------------------------------
# MCP discovery, risk, and execution
# ---------------------------------------------------------------------------

def test_mcp_tools_discovered_and_risk_applied(make_app, tmp_path):
    app = make_app.build(default="ok")
    _configure_mcp(app, tmp_path, autoapprove=False)
    names = app.tools.names()
    assert "mcp_test_echo_echo" in names
    assert "mcp_test_echo_add" in names
    spec = _spec(app, "mcp_test_echo_echo")
    assert spec.risk == "confirm"
    assert spec.module == "mcp:test_echo"
    assert spec.side_effects is True
    assert spec.read_only is False
    assert spec.untrusted_output is True
    assert app.modules.status["mcp:test_echo"].startswith("ok (")


def test_mcp_autoapprove_marks_low_risk(make_app, tmp_path):
    app = make_app.build(default="ok")
    _configure_mcp(app, tmp_path, autoapprove=True)
    spec = _spec(app, "mcp_test_echo_echo")
    assert spec.risk == "low"
    # Auto-approval does not make an unknown-provenance tool read-only.
    assert spec.side_effects is True
    assert spec.read_only is False


def test_mcp_call_round_trip(make_app, tmp_path):
    app = make_app.build(default="ok")
    _configure_mcp(app, tmp_path, autoapprove=True)
    ctx = app.tool_context("run_x", "tsk_x", None)
    outcome = app.tools.execute(
        _call("mcp_test_echo_echo", {"text": "round trip"}),
        ctx, run_id="run_x", task_id="tsk_x",
    )
    assert outcome["status"] == "succeeded"
    assert "round trip" in json.dumps(outcome["result"])


def test_mcp_tool_result_is_bounded(make_app, tmp_path):
    app = make_app.build(default="ok")
    _configure_mcp(app, tmp_path, autoapprove=True)
    ctx = app.tool_context("run_x", "tsk_x", None)
    app.config.set("limits.max_tool_result_chars", 300)
    outcome = app.tools.execute(
        _call("mcp_test_echo_echo", {"text": "x" * 100000}),
        ctx, run_id="run_x", task_id="tsk_x",
    )
    assert outcome["status"] == "succeeded"
    row = app.db.query_one(
        "SELECT result FROM tool_executions WHERE tool='mcp_test_echo_echo'"
    )
    assert len(row["result"] or "") < 400


def test_missing_mcp_config_is_unavailable_not_fatal(make_app, tmp_path):
    app = make_app.build(default="ok")
    app.config.set("tools.mcp_servers_file", str(tmp_path / "nope.yaml"))
    app.modules.reload()  # must not raise
    events = [e["type"] for e in app.events.list()]
    assert "mcp.config_missing" in events


def test_broken_mcp_server_is_unavailable_not_fatal(make_app, tmp_path):
    app = make_app.build(default="ok")
    bad = tmp_path / "bad_server.py"
    bad.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    import yaml

    config_path = tmp_path / "mcp.yaml"
    config_path.write_text(yaml.safe_dump({
        "servers": [{"name": "broken", "command": sys.executable,
                     "args": [str(bad)], "autoapprove": True}]
    }), encoding="utf-8")
    app.config.set("tools.mcp_servers_file", str(config_path))
    app.modules.reload()  # must not raise
    assert "mcp:broken" in app.modules.status
    assert "unavailable" in app.modules.status["mcp:broken"]
    # Cached/builtin capability still works; a turn is never blocked.
    result = app.runtime.submit_user_message("hello")
    assert result.status == "completed"


# ---------------------------------------------------------------------------
# Domain scope, reload, and budget
# ---------------------------------------------------------------------------

def test_domain_bound_module_is_invisible_and_uncallable_elsewhere(
    make_app, tmp_path
):
    app = make_app.build(default="ok")
    _configure_mcp(app, tmp_path, autoapprove=True, domains=["dom_a"])

    assert "mcp:test_echo" in app.modules.eligible_module_ids("dom_a")
    assert "mcp:test_echo" not in app.modules.eligible_module_ids("dom_b")

    selection = app.tools.schemas_for(
        "dom_b", budget_tokens=100000,
        eligible_modules=app.modules.eligible_module_ids("dom_b"),
    )
    assert "mcp_test_echo_echo" not in selection["names"]
    assert any(
        exclusion["module"] == "mcp:test_echo"
        and exclusion["reason"] == "module_not_eligible"
        for exclusion in selection["exclusions"]
    )

    # Hiding the schema is not enough: a call by name is denied, not run.
    ctx = app.tool_context("run_x", "tsk_x", "dom_b")
    outcome = app.tools.execute(
        _call("mcp_test_echo_echo", {"text": "should not run"}),
        ctx, run_id="run_x", task_id="tsk_x",
        allowed_names=set(selection["names"]),
    )
    assert outcome["status"] == "denied"
    assert "not available in this run" in outcome["error"]


def test_domain_change_recomputes_the_run_toolset_once(make_app, tmp_path):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "use_domain",
                         "arguments": {"domain": "Alpha"}}]},
        {"content": "Moved to Alpha."},
    ])
    app.domains.create("dom_a", name="Alpha")
    _configure_mcp(app, tmp_path, autoapprove=True, domains=["dom_a"])

    result = app.runtime.submit_user_message("do some work")
    assert result.status == "completed"
    task = app.tasks.get(result.task_id)
    assert task["domain_id"] == "dom_a"

    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    sources = util.loads(run["context_sources"], [])
    assert any(source["ref"] == "toolset.changed" for source in sources)
    events = [event["type"] for event in app.events.list()]
    assert "toolset.changed" in events

    # The second prompt (after use_domain) disclosed the newly eligible module.
    second_tools = {
        tool["function"]["name"] for tool in app.model.calls[1]["tools"]
    }
    assert "mcp_test_echo_echo" in second_tools


def test_reload_changes_the_registered_toolset_without_restart(
    make_app, tmp_path
):
    app = make_app.build(default="ok")
    _configure_mcp(app, tmp_path, autoapprove=True, tools_allow=["echo"])
    assert "mcp_test_echo_echo" in app.tools.names()
    assert "mcp_test_echo_add" not in app.tools.names()

    module = app.modules.get("mcp:test_echo")
    module.manifest["tools_allow"] = ["add"]
    app.modules.save(module)
    app.modules.reload("mcp:test_echo")

    assert "mcp_test_echo_add" in app.tools.names()
    assert "mcp_test_echo_echo" not in app.tools.names()


def test_schema_budget_excludes_low_priority_module_and_records_why(
    make_app, tmp_path
):
    app = make_app.build(default="ok")
    app.config.set("web.enabled", False)
    _configure_mcp(app, tmp_path, autoapprove=True)

    builtin_tokens = sum(
        util.token_len(util.dumps(app.tools._schema(spec)))
        for spec in app.tools.specs() if spec.module == "builtin"
    )
    selection = app.tools.schemas_for(None, budget_tokens=builtin_tokens)
    assert "mcp_test_echo_echo" not in selection["names"]
    dropped = [
        exclusion for exclusion in selection["exclusions"]
        if exclusion["tool"] == "mcp_test_echo_echo"
    ]
    assert dropped and dropped[0]["reason"] == "tool_schema_budget"
    assert "budget" in dropped[0]["detail"]


# ---------------------------------------------------------------------------
# Web module against a local SearXNG stub
# ---------------------------------------------------------------------------

class _SearchHandler(BaseHTTPRequestHandler):
    results: list[dict] = []
    malformed = False

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.server.malformed:  # type: ignore[attr-defined]
            body = b"this is not json"
        else:
            body = json.dumps({
                "results": self.server.results,  # type: ignore[attr-defined]
            }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep the test output clean
        return


@contextlib.contextmanager
def _searx_stub(results: list[dict] | None = None, malformed: bool = False):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SearchHandler)
    server.results = results or [  # type: ignore[attr-defined]
        {"title": "Example", "url": "http://example.test/page1",
         "content": "A bounded snippet."},
    ]
    server.malformed = malformed  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _web_search(app, query: str = "test") -> dict:
    selection = app.tools.schemas_for(None, budget_tokens=100000)
    ctx = app.tool_context("run_x", "tsk_x", None)
    return app.tools.execute(
        _call("web_search", {"query": query}), ctx,
        run_id="run_x", task_id="tsk_x",
        allowed_names=set(selection["names"]),
    )


def test_web_search_is_bounded_structured_and_untrusted(make_app):
    app = make_app.build(default="ok")
    many = [
        {"title": "t" * 500, "url": f"http://example.test/{i}",
         "content": "s" * 5000}
        for i in range(20)
    ]
    with _searx_stub(results=many) as url:
        app.config.set("web.searxng_url", url)
        app.modules.reload("web")
        outcome = _web_search(app, "bounded")

    assert outcome["status"] == "succeeded"
    result = outcome["result"]
    assert result["count"] <= 5
    assert len(result["results"]) <= 5
    for item in result["results"]:
        assert len(item["snippet"]) <= 400
        assert len(item["title"]) <= 200
    assert len(result["sources"]) == result["count"]

    spec = app.tools.resolve("web_search")
    assert spec.risk == "low"
    assert spec.read_only is True
    assert spec.side_effects is False
    assert spec.untrusted_output is True


def test_web_search_malformed_json_is_a_clean_error(make_app):
    app = make_app.build(default="ok")
    with _searx_stub(malformed=True) as url:
        app.config.set("web.searxng_url", url)
        app.modules.reload("web")
        outcome = _web_search(app, "broken")
    assert outcome["status"] == "failed"
    assert "malformed" in outcome["error"]
    # No traceback leaked into the observation.
    assert "Traceback" not in outcome["error"]


# ---------------------------------------------------------------------------
# Taint: untrusted web content escalates a side-effecting tool
# ---------------------------------------------------------------------------

def test_taint_escalates_side_effecting_tool_and_names_the_source(
    make_app, tmp_path
):
    app = make_app.build(scripts=[
        {"tool_calls": [{"name": "web_search",
                         "arguments": {"query": "anything"}}]},
        {"tool_calls": [{"name": "mcp_test_echo_echo",
                         "arguments": {"text": "send it"}}]},
        {"content": "done"},
    ])
    _configure_mcp(app, tmp_path, autoapprove=True)  # low risk, side effects
    with _searx_stub() as url:
        app.config.set("web.searxng_url", url)
        app.modules.reload("web")
        result = app.runtime.submit_user_message("search then act")

    assert result.status == "waiting_approval"
    run = app.db.query_one("SELECT * FROM runs WHERE id=?", (result.run_id,))
    meta = util.loads(run["meta"], {})
    assert meta["tainted_sources"]
    assert meta["tainted_sources"][0]["tool"] == "web_search"
    assert meta["tainted_sources"][0]["origin"].startswith(
        "http://example.test/"
    )

    approval = app.approvals.pending()[0]
    assert approval["tool"] == "mcp_test_echo_echo"
    assert approval["risk"] == "confirm"
    assert "untrusted content" in approval["reason"]
    assert "http://example.test/" in approval["reason"]
    assert "untrusted content" in result.message

    row = app.db.query_one(
        "SELECT status FROM tool_executions WHERE tool='mcp_test_echo_echo'"
    )
    assert row["status"] == "awaiting_approval"
    events = [event["type"] for event in app.events.list()]
    assert "trust.tainted" in events
    assert "trust.escalated" in events


# ---------------------------------------------------------------------------
# Skills (unchanged until Phase 2)
# ---------------------------------------------------------------------------

def test_skills_disclosed_by_preconditions(make_app, tmp_path):
    app = make_app.build(default="ok")
    skills_dir = tmp_path / "skills"
    (skills_dir / "weekly-review").mkdir(parents=True)
    (skills_dir / "weekly-review" / "SKILL.md").write_text(
        "---\nname: weekly-review\ndescription: weekly planning\n"
        "preconditions: weekly, review, planning\n---\n"
        "List open tasks first.\n",
        encoding="utf-8",
    )
    app.config.set("tools.skills_dir", str(skills_dir))
    task = app.tasks.create(objective="let us do the weekly planning review",
                            origin="user")
    built = app.context.build(task=task, domain=None, trigger={"kind": "user"})
    assert "weekly-review" in built["system"]
    # Non-matching objective: skill withheld with a recorded reason.
    task2 = app.tasks.create(objective="make coffee", origin="user")
    built2 = app.context.build(task=task2, domain=None, trigger={"kind": "user"})
    assert "weekly-review" not in built2["system"]
    reasons = [s["reason"] for s in built2["sources"]
               if s["section"] == "skills"]
    assert any("preconditions" in r for r in reasons)

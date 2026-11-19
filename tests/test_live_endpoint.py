"""H. Real model endpoint integration tests.

These call the configured endpoint at http://192.168.50.40:8181/ and are
skipped by default; run them explicitly with:

    python -m pytest tests/test_live_endpoint.py -m live -o addopts=""

The endpoint must be reachable for these to run; a skip is an environment
condition, not a pass.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from harness.config import load_config

pytestmark = pytest.mark.live
ENDPOINT = "http://192.168.50.40:8181"


def _live_client(tmp_path):
    from harness.model import ModelClient

    config = load_config(tmp_path)
    config.set("model.base_url", ENDPOINT)
    config.set("model.name", "qwen3.5-9b")
    return ModelClient(config)


def test_endpoint_lists_configured_model(tmp_path):
    client = _live_client(tmp_path)
    try:
        result = client.probe()
        assert result["reachable"], result["detail"]
        assert result["model_listed"] is True
        assert "qwen3.5-9b" in result.get("models", [])
    finally:
        client.close()


def test_endpoint_plain_completion(tmp_path):
    client = _live_client(tmp_path)
    try:
        reply = client.chat(
            [{"role": "user", "content":
              "Reply with exactly the word: banana"}],
            tools=[], max_tokens=256,
        )
        assert reply.content.strip()
        assert "banana" in reply.content.lower()
    finally:
        client.close()


def test_endpoint_native_tool_call_protocol(tmp_path):
    tools = [{
        "type": "function",
        "function": {
            "name": "probe_echo",
            "description": "Return the text argument unchanged",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }]
    client = _live_client(tmp_path)
    try:
        reply = client.chat(
            [{"role": "user",
              "content": "Call the probe_echo tool with text 'toolcheck'."}],
            tools=tools, max_tokens=1024,
        )
        assert reply.tool_calls, "endpoint did not return a native tool call"
        call = reply.tool_calls[0]
        assert call.name == "probe_echo"
        assert json.loads(call.raw_arguments).get("text") == "toolcheck"
    finally:
        client.close()


def test_endpoint_multi_step_tool_round_trip(tmp_path):
    tools = [{
        "type": "function",
        "function": {
            "name": "probe_add",
            "description": "Add two integers a and b",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "integer"},
                               "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
        },
    }]
    client = _live_client(tmp_path)
    try:
        first = client.chat(
            [{"role": "user",
              "content": "What is 17 plus 25? Use the probe_add tool."}],
            tools=tools, max_tokens=1024,
        )
        assert first.tool_calls
        call = first.tool_calls[0]
        args = call.arguments if isinstance(call.arguments, dict) else {}
        result = {"result": int(args.get("a", 0)) + int(args.get("b", 0))}
        messages = [
            {"role": "user",
             "content": "What is 17 plus 25? Use the probe_add tool."},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": call.id, "type": "function",
                             "function": {"name": call.name,
                                          "arguments": call.raw_arguments}}]},
            {"role": "tool", "tool_call_id": call.id,
             "content": json.dumps(result)},
        ]
        second = client.chat(messages, tools=tools, max_tokens=512)
        assert "42" in second.content
    finally:
        client.close()


def test_live_calendar_correction_updates_existing_uid(tmp_path):
    """A real-model correction uses the linked mutation receipt, not recreation."""
    from harness import util
    from harness.app import App
    from harness.config import load_config
    from harness.tools import ToolSpec

    config = load_config(tmp_path)
    config.set("model.base_url", ENDPOINT)
    config.set("model.name", "qwen3.5-9b")
    app = App(tmp_path, config=config)
    calls = []
    uid = "office-day@personal-harness"

    def event_schema(name, required):
        return {
            "type": "object",
            "properties": {
                "uid": {"type": "string"},
                "summary": {"type": "string"},
                "calendar": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "all_day": {"type": "boolean"},
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": required,
        }

    def update(ctx, arguments):
        calls.append(("update", dict(arguments)))
        return {
            "status": "updated", "uid": uid,
            "summary": arguments.get("summary") or "Office day",
            "calendar": "Personal", "start": "2026-11-19T00:00:00-06:00",
            "end": "2026-11-20T00:00:00-06:00", "all_day": True,
        }

    def create(ctx, arguments):
        calls.append(("create", dict(arguments)))
        return {"status": "created", "uid": "duplicate@personal-harness"}

    def list_events(ctx, arguments):
        calls.append(("list", dict(arguments)))
        return {
            "count": 1, "events": [{
                "uid": uid, "summary": "Office day", "calendar": "Personal",
                "start": "2026-11-19T00:00:00-06:00",
                "end": "2026-11-20T00:00:00-06:00", "all_day": True,
            }],
            "sources": ["https://calendar.test/office-day.ics"],
        }

    app.tools.register(ToolSpec(
        name="calendar_update_event", description="Update an event by exact UID",
        parameters=event_schema("update", ["uid"]), risk="confirm",
        handler=update, side_effects=True, untrusted_output=True,
    ))
    app.tools.register(ToolSpec(
        name="calendar_create_event", description="Create a separate new event",
        parameters=event_schema("create", ["summary", "start", "end"]),
        risk="confirm", handler=create, side_effects=True,
        untrusted_output=True,
    ))
    app.tools.register(ToolSpec(
        name="calendar_list_events", description="List existing calendar events",
        parameters=event_schema("list", []), risk="low", handler=list_events,
        read_only=True, untrusted_output=True,
    ))
    app.startup()
    try:
        previous = app.tasks.create(
            objective='Create an event called "Office day" on Thursday',
            origin="user",
        )
        run_id = util.new_id("run")
        app.db.execute(
            "INSERT INTO runs(id, task_id, trigger, status, started_at, ended_at,"
            " transcript) VALUES (?,?,?,?,?,?,?)",
            (run_id, previous["id"], "user", "completed", util.iso(),
             util.iso(), "[]"),
        )
        app.tasks.set_status(previous["id"], "completed", reason="done")
        app.messages.record(
            "user", previous["objective"], source="web", task_id=previous["id"],
        )
        app.messages.record(
            "assistant", 'Created "Office day" in Personal as an all-day event.',
            source="agent", task_id=previous["id"], run_id=run_id,
        )
        app.db.execute(
            "INSERT INTO tool_executions(id, run_id, task_id, tool, tool_call_id,"
            " arguments, status, started_at, ended_at, result) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "ex_office_create", run_id, previous["id"],
                "calendar_create_event", "call_office", "{}", "succeeded",
                util.iso(), util.iso(), util.dumps({
                    "status": "created", "uid": uid, "summary": "Office day",
                    "calendar": "Personal",
                    "start": "2026-11-19T00:00:00-06:00",
                    "end": "2026-11-20T00:00:00-06:00", "all_day": True,
                }),
            ),
        )
        result = app.runtime.submit_user_message(
            'No... I told you to change the name to "Office Day Pantry party"'
        )
        assert result.status == "waiting_approval", result.message
        approval = app.approvals.pending()[0]
        assert approval["tool"] == "calendar_update_event", approval
        assert approval["arguments"]["uid"] == uid
        assert approval["arguments"]["summary"] == "Office Day Pantry party"
        assert not any(kind == "create" for kind, _args in calls), calls
        app.approvals.decide(approval["id"], "approve", decided_by="test")
        resumed = app.runtime.resume_after_approval(approval, "test")
        assert resumed.status == "completed"
        assert [kind for kind, _args in calls].count("update") == 1
        assert not any(kind == "create" for kind, _args in calls), calls
        assert "Office Day Pantry party" in resumed.message
    finally:
        app.shutdown()


def test_live_unattended_research_uses_current_year_and_sources(tmp_path):
    """The real model follows the bounded date/query/report contract."""
    from harness import util
    from harness.app import App
    from harness.config import load_config
    from harness.tools import ToolSpec

    config = load_config(tmp_path)
    config.set("model.base_url", ENDPOINT)
    config.set("model.name", "qwen3.5-9b")
    app = App(tmp_path, config=config)
    calls = []

    def search(ctx, arguments):
        calls.append(dict(arguments))
        return {
            "results": [{
                "title": "Current model release",
                "url": "https://example.com/current-model-2026",
                "snippet": "A current model release with benchmark details.",
                "published_date": "2026-11-18",
            }],
            "sources": ["https://example.com/current-model-2026"],
        }

    app.tools.register(ToolSpec(
        name="web_search", description="Search current web news",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "categories": {"type": "string"},
                "max_results": {"type": "integer"},
                "time_range": {
                    "type": "string",
                    "description": "Recency: day | week | month | year",
                },
            },
            "required": ["query"],
        },
        risk="low", handler=search, external_egress=True,
        read_only=True, untrusted_output=True,
    ))
    app.startup()
    try:
        domain = app.domains.create(
            "dom_ai", name="ai_research", description="AI research and news",
            keywords=["AI", "model", "research", "news"],
            examples=["daily AI report", "frontier model updates"],
            created_by="test",
        )
        skill = app.modules.skills.create_draft(
            name="generate_ai_research_report",
            description="Generate a current sourced AI research report",
            domains=[domain["id"]],
            preconditions=["AI research", "model news"],
            body=(
                "Use every approved unattended web query exactly once with "
                "time_range=week. Use the occurrence date for current-year "
                "queries, prefer recent dated results, and finish with a concise "
                "report containing a source URL for every item. Do not delegate."
            ),
            created_by="model",
        )
        app.modules.skills.activate(skill.id)
        owner = app.tasks.create(
            objective="own AI report", domain_id=domain["id"], origin="user",
        )
        app.scheduler.create_for_task(
            owner["id"], name="Daily AI report",
            prompt=(
                "Run generate_ai_research_report: use both approved searches, "
                "then write a concise report with source links."
            ),
            kind="daily", time_of_day="12:00", tz="America/Mexico_City",
            target_domain_id=domain["id"],
            unattended={
                "web_queries": [
                    {"query": "local AI model hosting news {current_year}",
                     "time_range": "week", "max_results": 5},
                    {"query": "frontier AI model news {current_year}",
                     "time_range": "week", "max_results": 5},
                ],
                "allow_task_notes": False,
            },
        )
        schedule = app.scheduler.list_schedules()[0]
        app.db.execute(
            "UPDATE schedules SET next_due_at=? WHERE id=?",
            (util.iso(util.now_utc() - dt.timedelta(minutes=1)), schedule["id"]),
        )
        task = app.scheduler.submit_due()[0]["task"]
        result = app.runtime.start_run(
            task, {"kind": "schedule", "name": "Daily AI report"},
        )
        assert result.status == "completed", result.message
        assert len(calls) == 2, calls
        from harness.config import resolve_zone
        expected_year = util.now_utc().astimezone(
            resolve_zone("America/Mexico_City")
        ).strftime("%Y")
        assert all(expected_year in call["query"] for call in calls), calls
        assert not any(
            "2024" in call["query"] or "2025" in call["query"]
            for call in calls
        ), calls
        assert all(call.get("time_range") == "week" for call in calls), calls
        assert "https://example.com/current-model-2026" in result.message
        assert app.approvals.pending() == []
    finally:
        app.shutdown()


def test_live_end_to_end_turn(tmp_path):
    """One full harness turn against the real endpoint."""
    from harness.app import App
    from harness.config import load_config

    app = App(tmp_path, config=load_config(tmp_path))  # real ModelClient
    app.startup()
    try:
        result = app.runtime.submit_user_message(
            "Say hello and tell me what day type it is (weekday or weekend) "
            "in one short sentence.", source="web",
        )
        assert result.status == "completed"
        assert result.message.strip()
    finally:
        app.shutdown()

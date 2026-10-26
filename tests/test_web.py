from __future__ import annotations

import json

import anyio
import httpx
import pytest

pytest.importorskip("jinja2")

from harness.contracts.domains import StructureDraft  # noqa: E402
from harness.contracts.memory import MemoryCandidate  # noqa: E402
from harness.contracts.tasks import TaskSpec  # noqa: E402
from harness.ui.web import create_app  # noqa: E402
from tests.conftest import act, plan, respond  # noqa: E402


def _request(harness, method: str, path: str = "/memory", body: str = "") -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=create_app(harness))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(
                method,
                path,
                content=body,
                headers={
                    "authorization": f"Bearer {harness.settings.get('ui.access_token')}",
                    "content-type": "application/x-www-form-urlencoded",
                },
            )

    return anyio.run(run)


def _sse_events(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if name:
            events.append((name, json.loads(data)))
    return events


def _memory(
    harness,
    *,
    content: str = "The user prefers concise answers",
    interaction_id: str | None = None,
) -> MemoryCandidate:
    interaction = (
        harness.sessions.get_interaction(interaction_id)
        if interaction_id
        else harness.sessions.record_interaction("I prefer concise answers")
    )
    if interaction is None:
        interaction = harness.sessions.record_interaction("I prefer concise answers")
    return MemoryCandidate(
        content=content,
        type_hint="preference",
        scope_hint="global",
        default_scope="global",
        provenance={
            "source_kind": "user_statement",
            "source_refs": [interaction.id],
            "evidence_status": "stated",
        },
    )


def test_approve_endpoint_is_gone_and_returns_400(make_harness) -> None:
    h = make_harness([])
    record = h.memory.submit(_memory(h)).record
    assert record is not None

    response = _request(h, "POST", body=f"id={record.id}&action=approve")
    assert response.status_code == 400
    assert "unknown memory action: approve" in response.text
    assert h.memory.get(record.id).status == "active"


def test_retract_active_memory_redirects(make_harness) -> None:
    h = make_harness([])
    record = h.memory.submit(_memory(h)).record
    assert record is not None

    response = _request(h, "POST", body=f"id={record.id}&action=retract")
    assert response.status_code == 303
    assert h.memory.get(record.id).status == "retracted"


def test_unknown_memory_id_returns_404(make_harness) -> None:
    h = make_harness([])
    response = _request(h, "POST", body="id=mem_missing&action=retract")
    assert response.status_code == 404
    assert "memory not found" in response.text


def test_memory_page_has_no_approval_queue(make_harness) -> None:
    h = make_harness([])
    h.memory.submit(_memory(h))
    response = _request(h, "GET")
    assert response.status_code == 200
    assert "Awaiting approval" not in response.text
    assert ">approve<" not in response.text
    assert ">reject<" not in response.text


def test_rollback_run_undoes_a_consolidation_run_from_the_memory_page(make_harness) -> None:
    h = make_harness([], settings={"consolidation": {"extract_enabled": False}})
    run = h.consolidation.run(trigger="test")
    response = _request(h, "POST", body=f"run_id={run.id}&action=rollback_run")
    assert response.status_code == 303
    assert h.consolidation.get(run.id).status == "rolled_back"


def test_unknown_consolidation_action_returns_400(make_harness) -> None:
    h = make_harness([])
    response = _request(h, "POST", "/consolidation", "action=nope")
    assert response.status_code == 400
    assert "unknown consolidation action" in response.text


def test_history_page_lists_stored_messages_even_when_prompt_drops_them(make_harness) -> None:
    h = make_harness([], settings={"context": {"budgets": {"history": 20}}})
    item = h.sessions.record_interaction("a stored message that the prompt may drop")
    h.sessions.append_assistant(item.id, "a stored reply")

    response = _request(h, "GET", "/history")
    assert response.status_code == 200
    assert "Conversation history" in response.text
    assert "Stored messages" in response.text
    assert "a stored message that the prompt may drop" in response.text
    assert "a stored reply" in response.text


def test_chat_page_renders_messages_and_trace_links(make_harness) -> None:
    h = make_harness([plan("hello"), respond("Hello from chat.")])
    result = h.chat.submit("hello")

    response = _request(h, "GET", "/chat")
    assert response.status_code == 200
    assert "chat-form" in response.text
    assert "hello" in response.text
    assert "Hello from chat." in response.text
    assert f"/traces/trc_" in response.text
    assert f'id="msg-{result.interaction_id}"' in response.text


def test_chat_page_is_a_scrollable_panel_with_the_composer_inside(make_harness) -> None:
    h = make_harness([plan("hello"), respond("Hello from chat.")])
    h.chat.submit("hello")

    response = _request(h, "GET", "/chat")
    assert response.status_code == 200
    page = response.text
    assert '<body class="chat-page">' in page
    assert "class='chat-panel'" in page
    panel = page.split("class='chat-panel'", 1)[1]
    log_at = panel.index("id='chat-log'")
    live_at = panel.index("id='live'")
    composer_at = panel.index("id='chat-composer'")
    assert log_at < live_at < composer_at
    assert "id='chat-form'" in panel[composer_at:]
    assert "#chat-log{flex:1 1 auto;min-height:0;overflow-y:auto" in page
    assert "#chat-text{resize:none;max-height:12em;overflow-y:auto" in page
    assert "#live-stages{max-height:12em;overflow:auto}" in page
    assert "#live-tokens{max-height:12em;overflow:auto" in page
    assert page.count("scrollLog();") >= 2


def test_chat_shows_active_domain_and_selector_outside_the_stream(make_harness) -> None:
    h = make_harness([])
    domain = h.domains.create_domain(
        StructureDraft(name="Home Lab", reason="test", evidence="test"), confirmed=True
    )
    h.sessions.set_active_domain(domain.id)

    page = _request(h, "GET", "/chat")
    assert page.status_code == 200
    assert "Active domain: Home Lab" in page.text
    assert "action='/chat/domain'" in page.text
    assert f"value='{domain.id}' selected" in page.text
    assert "EventSource" in page.text  # selector is outside the SSE path

    cleared = _request(h, "POST", "/chat/domain", "domain_id=")
    assert cleared.status_code == 303
    assert h.sessions.active_domain() is None
    assert "Active domain: none" in _request(h, "GET", "/chat").text

    selected = _request(h, "POST", "/chat/domain", f"domain_id={domain.id}")
    assert selected.status_code == 303
    assert h.sessions.active_domain() == domain.id


def test_non_chat_pages_keep_the_normal_document_flow(make_harness) -> None:
    h = make_harness([])
    response = _request(h, "GET", "/memory")
    assert response.status_code == 200
    assert 'class="chat-page"' not in response.text
    assert "class='chat-panel'" not in response.text


def test_chat_completed_turn_appends_without_reloading_the_document(make_harness) -> None:
    h = make_harness([plan("hello"), respond("Hello appended.")])
    result = h.chat.submit("hello")

    page = _request(h, "GET", "/chat")
    assert "window.location.reload" not in page.text
    assert "'turn.completed'" in page.text
    assert "/chat/interactions/" in page.text

    fragment = _request(h, "GET", f"/chat/interactions/{result.interaction_id}")
    assert fragment.status_code == 200
    assert f'id="msg-{result.interaction_id}"' in fragment.text
    assert "Hello appended." in fragment.text
    assert "/traces/trc_" in fragment.text
    assert "chat-form" not in fragment.text
    assert "<html" not in fragment.text


def test_chat_interaction_fragment_preserves_memory_undo(make_harness) -> None:
    h = make_harness(
        [
            plan("remember bluefin"),
            act("harness_memory", "remember", content="the code word is bluefin"),
            respond("Noted."),
        ]
    )
    result = h.chat.submit("remember bluefin")
    record = h.memory.list()[0]

    fragment = _request(h, "GET", f"/chat/interactions/{result.interaction_id}")
    assert fragment.status_code == 200
    assert "memory writes" in fragment.text
    assert 'action="/chat/memory/undo"' in fragment.text
    assert f'value="{record.id}"' in fragment.text
    assert ">undo<" in fragment.text


def test_missing_chat_interaction_fragment_returns_404(make_harness) -> None:
    h = make_harness([])
    response = _request(h, "GET", "/chat/interactions/int_missing")
    assert response.status_code == 404
    assert "interaction not found" in response.text


def test_chat_turn_rejects_empty_text(make_harness) -> None:
    h = make_harness([])
    response = _request(h, "POST", "/chat/turn", "text=")
    assert response.status_code == 400
    assert response.json()["error"] == "message text is required"


def test_chat_stream_reports_stages_tokens_and_completion(make_harness) -> None:
    h = make_harness([plan("hello"), respond("Hello streamed.")])

    async def run() -> tuple[int, str]:
        transport = httpx.ASGITransport(app=create_app(h))
        headers = {"authorization": f"Bearer {h.settings.get('ui.access_token')}"}
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = await client.post(
                "/chat/turn",
                content="text=hello",
                headers={**headers, "content-type": "application/x-www-form-urlencoded"},
            )
            assert started.status_code == 202
            job_id = started.json()["job_id"]
            async with client.stream("GET", f"/chat/turns/{job_id}/events", headers=headers) as response:
                body = (await response.aread()).decode()
                return response.status_code, body

    status, body = anyio.run(run)
    assert status == 200
    events = _sse_events(body)
    names = [name for name, _ in events]
    assert "turn.started" in names
    assert "stage" in names
    assert "token" in names
    assert "turn.completed" in names
    completed = next(data for name, data in events if name == "turn.completed")
    assert completed["messages"] == ["Hello streamed."]
    assert completed["trace_ids"]
    plan_stages = [data["stage"] for name, data in events if name == "stage"]
    assert "plan.task_create" in plan_stages
    assert "model_call" in plan_stages
    assert "turn.response" in plan_stages


def test_trace_detail_links_back_to_the_chat_message(make_harness) -> None:
    h = make_harness([plan("hello"), respond("Hello.")])
    result = h.chat.submit("hello")
    trace = next(
        item for item in h.traces.list_for(interaction_id=result.interaction_id) if item.role == "turn"
    )

    response = _request(h, "GET", f"/traces/{trace.id}")
    assert response.status_code == 200
    assert f"/chat?interaction={result.interaction_id}" in response.text
    assert f"#msg-{result.interaction_id}" in response.text


def test_missing_trace_returns_404(make_harness) -> None:
    h = make_harness([])
    response = _request(h, "GET", "/traces/trc_missing")
    assert response.status_code == 404
    assert "trace not found" in response.text


def test_chat_shows_memory_write_and_undo_retracts_it(make_harness) -> None:
    h = make_harness(
        [
            plan("remember bluefin"),
            act("harness_memory", "remember", content="the code word is bluefin"),
            respond("Noted."),
        ]
    )
    result = h.chat.submit("remember bluefin")
    record = h.memory.list()[0]

    page = _request(h, "GET", "/chat")
    assert page.status_code == 200
    assert "memory writes" in page.text
    assert record.id in page.text
    assert ">undo<" in page.text

    undo = _request(
        h,
        "POST",
        "/chat/memory/undo",
        f"memory_id={record.id}&interaction={result.interaction_id}",
    )
    assert undo.status_code == 303
    assert h.memory.get(record.id).status == "retracted"


def test_failed_turn_emits_visible_error_and_page_shows_it(make_harness) -> None:
    h = make_harness([plan("hello"), RuntimeError("backend exploded")])
    events = []
    result = h.chat.submit("hello", events.append)
    assert result.error is not None
    assert "backend exploded" in result.error

    failed = [event for event in events if event.kind == "turn.failed"]
    assert len(failed) == 1
    assert failed[0].data["interaction_id"] == result.interaction_id
    assert "backend exploded" in failed[0].data["error"]

    page = _request(h, "GET", "/chat")
    assert page.status_code == 200
    assert "Turn failed:" in page.text
    assert "backend exploded" in page.text


def test_turn_detail_many_covers_all_messages_and_falls_back(make_harness) -> None:
    h = make_harness([plan("one"), respond("first"), plan("two"), respond("second")])
    first = h.chat.submit("one")
    second = h.chat.submit("two")
    ids = [first.interaction_id, second.interaction_id]

    grouped = h.chat.detail_many(ids)
    assert set(grouped) == set(ids)
    grouped_traces = {key: [trace.id for trace in detail.traces] for key, detail in grouped.items()}
    assert all(grouped_traces[key] for key in ids)

    h.settings.set("ui.chat_trace_scan", 0, actor="test")
    fallback = h.chat.detail_many(ids)
    assert {
        key: [trace.id for trace in detail.traces] for key, detail in fallback.items()
    } == grouped_traces

    # A scan smaller than the visible history keeps the newest turn grouped and
    # falls back to a per-interaction scan for the older one.
    h.settings.set("ui.chat_trace_scan", 3, actor="test")
    partial = h.chat.detail_many(ids)
    assert {
        key: [trace.id for trace in detail.traces] for key, detail in partial.items()
    } == grouped_traces


def test_duplicate_remember_is_not_shown_as_an_undoable_write(make_harness) -> None:
    h = make_harness(
        [
            plan("remember bluefin"),
            act("harness_memory", "remember", content="the code word is bluefin"),
            respond("Noted."),
            plan("remember bluefin again"),
            act("harness_memory", "remember", content="the code word is bluefin"),
            respond("It was already noted."),
        ]
    )
    h.chat.submit("remember bluefin")
    h.chat.submit("remember bluefin again")

    page = _request(h, "GET", "/chat")
    assert page.status_code == 200
    assert page.text.count(">undo<") == 1
    assert h.memory.list()[0].status == "active"


def test_chat_shows_retrieved_memories(make_harness) -> None:
    h = make_harness(
        [
            plan("what is the bluefin code word"),
            respond("It is bluefin."),
        ]
    )
    interaction = h.sessions.record_interaction("I told you the bluefin code word")
    h.memory.submit(_memory(h, content="the bluefin code word is bluefin", interaction_id=interaction.id))
    h.chat.submit("what is the bluefin code word")

    page = _request(h, "GET", "/chat")
    assert page.status_code == 200
    assert "memories retrieved" in page.text
    assert "the bluefin code word is bluefin" in page.text


def test_domains_page_lists_only_domains_and_keeps_actions(make_harness) -> None:
    h = make_harness([])
    domain = h.domains.create_domain(
        StructureDraft(name="Home Lab", reason="test", evidence="test"), confirmed=True
    )
    suggestion = h.domains.record_suggestion(
        StructureDraft(name="Future Lab", reason="test", evidence="test")
    )

    page = _request(h, "GET", "/domains")
    assert page.status_code == 200
    assert "Home Lab" in page.text
    assert suggestion.id in page.text
    assert "Projects" not in page.text
    assert "memory scope" not in page.text
    assert ">rename<" in page.text and ">archive<" in page.text
    assert ">confirm<" in page.text and ">decline<" in page.text

    renamed = _request(h, "POST", "/domains", f"id={domain.id}&action=rename&name=Home+Lab")
    assert renamed.status_code == 303
    assert h.domains.get_domain(domain.id).name == "Home Lab"

    archived = _request(h, "POST", "/domains", f"id={domain.id}&action=archive")
    assert archived.status_code == 303
    assert h.domains.get_domain(domain.id).archived is True


def test_tasks_page_has_no_project_column(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="check nas"))

    page = _request(h, "GET", "/tasks")
    assert page.status_code == 200
    assert task.id in page.text
    assert "check nas" in page.text
    assert "<th>project</th>" not in page.text


def test_tasks_page_shows_work_status(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="check nas"))

    page = _request(h, "GET", "/tasks")
    assert page.status_code == 200
    assert "Work status" in page.text
    assert "Ready next" in page.text
    assert task.id in page.text

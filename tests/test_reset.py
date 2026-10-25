from __future__ import annotations

import json
import threading
import zipfile
from pathlib import Path

import anyio
import httpx
import pytest

pytest.importorskip("jinja2")

from harness.app import build_harness
from harness.contracts.memory import MemoryCandidate
from harness.model.scripted import ScriptedAdapter
from harness.ui.web import create_app
from tests.conftest import TEST_BASELINE, act, plan, respond, write_settings


def _request(harness, method: str, path: str, body: str = "") -> httpx.Response:
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


def test_full_reset_wipes_reseeds_and_next_conversation_works(make_harness) -> None:
    h = make_harness(
        [
            plan("remember bluefin"),
            act("harness_memory", "remember", content="the code word is bluefin"),
            respond("Noted."),
        ]
    )
    h.chat.submit("remember bluefin")
    assert h.memory.list()
    assert h.tasks.list()
    assert h.sessions.list_interactions(limit=None)

    h.model.push(respond("Fresh hello."))
    report = h.reset.run("full")

    assert report.backup_path is not None
    archive = Path(report.backup_path)
    assert archive.exists()
    names = zipfile.ZipFile(archive).namelist()
    assert any(name.startswith("memory/records/") for name in names)
    assert any(name.startswith("sessions/") for name in names)

    assert h.memory.list() == []
    assert h.tasks.list() == []
    assert h.sessions.list_interactions(limit=None) == []
    assert [item.id for item in h.domains.list_domains()] == ["self"]
    assert h.profile.current().name == "Assistant"
    assert h.settings.get("tasks.planning_mode") == "heuristic"
    assert h.settings.get("maintenance.reset.backup") is True

    result = h.chat.submit("hello")
    assert result.error is None
    assert result.messages == ["Fresh hello."]
    stored = h.sessions.list_interactions(limit=None)
    assert [item.user_text for item in stored] == ["hello"]
    assert [item.assistant_text for item in stored] == ["Fresh hello."]
    assert h.traces.list_for(interaction_id=result.interaction_id)


def test_memories_only_reset_keeps_conversation_and_clears_memory_links(make_harness) -> None:
    h = make_harness(
        [
            plan("remember bluefin"),
            act("harness_memory", "remember", content="the code word is bluefin"),
            respond("Noted."),
        ]
    )
    result = h.chat.submit("remember bluefin")
    record = h.memory.list()[0]
    assert h.tasks.list()

    report = h.reset.run("memories")

    assert report.backup_path is not None
    assert h.memory.list() == []
    assert h.consolidation.list() == []
    assert h.tasks.list()
    assert h.sessions.get_interaction(result.interaction_id) is not None
    assert h.events.query(operation="memory.commit") == []
    for path in (h.data_dir / "traces").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert not [
            row
            for row in data.get("included") or []
            if str(row.get("item") or "").startswith("mem_")
        ]
        envelope = (data.get("validation") or {}).get("envelope") or {}
        assert record.id not in json.dumps(envelope)


def test_conversations_only_reset_keeps_memories_and_drops_dangling_refs(make_harness) -> None:
    h = make_harness(
        [
            plan("remember bluefin"),
            act("harness_memory", "remember", content="the code word is bluefin"),
            respond("Noted."),
        ]
    )
    interaction = h.sessions.record_interaction("remember bluefin")
    submitted = h.memory.submit(
        MemoryCandidate(
            content="the code word is bluefin",
            type_hint="fact",
            scope_hint="global",
            default_scope="global",
            provenance={
                "source_kind": "user_statement",
                "source_refs": [interaction.id],
                "evidence_status": "stated",
            },
        )
    )
    record = submitted.record
    assert record is not None
    old_session = h.sessions.current().id
    h.chat.submit("hello")

    report = h.reset.run("conversations")

    assert report.backup_path is not None
    assert h.memory.get(record.id).status == "active"
    assert h.memory.get(record.id).provenance["source_refs"] == []
    assert h.memory.get(record.id).provenance["reset_cleared_refs"] == [interaction.id]
    assert h.sessions.list_interactions(limit=None) == []
    assert h.sessions.current().id != old_session
    assert h.tasks.list() == []
    assert h.history.list_segments() == []
    assert h.traces.list_for(limit=50) == []

    h.model.push(plan("hi"))
    h.model.push(respond("Hi."))
    result = h.chat.submit("hi")
    assert result.error is None
    assert result.messages == ["Hi."]


def test_ui_reset_requires_confirmation_and_honors_backup_setting(make_harness) -> None:
    h = make_harness(
        [
            plan("remember bluefin"),
            act("harness_memory", "remember", content="the code word is bluefin"),
            respond("Noted."),
        ]
    )
    h.chat.submit("remember bluefin")
    record = h.memory.list()[0]

    page = _request(h, "GET", "/reset")
    assert page.status_code == 200
    assert "Type <b>RESET</b>" in page.text

    refused = _request(h, "POST", "/reset", "scope=memories&confirm=nope&backup=on")
    assert refused.status_code == 400
    assert "confirmation word does not match" in refused.text
    assert h.memory.get(record.id).status == "active"

    h.settings.set("maintenance.reset.backup", False, "test")
    done = _request(h, "POST", "/reset", "scope=memories&confirm=RESET")
    assert done.status_code == 200
    assert "Reset complete: memories" in done.text
    assert "Backup disabled" in done.text
    assert h.memory.list() == []
    assert not (h.data_dir / "backups").exists()


def test_ui_refuses_reset_when_confirmation_word_is_empty(make_harness) -> None:
    h = make_harness(
        [
            plan("remember bluefin"),
            act("harness_memory", "remember", content="the code word is bluefin"),
            respond("Noted."),
        ]
    )
    h.chat.submit("remember bluefin")
    record = h.memory.list()[0]
    h.settings.set("maintenance.reset.confirmation_word", "  ", "test")

    page = _request(h, "GET", "/reset")
    assert page.status_code == 400
    assert "confirmation word is empty" in page.text

    blocked = _request(h, "POST", "/reset", "scope=full&confirm=&backup=on")
    assert blocked.status_code == 400
    assert h.memory.get(record.id).status == "active"


class _BlockingAdapter:
    def __init__(self, inner, started: threading.Event, release: threading.Event):
        self._inner = inner
        self._started = started
        self._release = release
        self._first = True

    def complete(self, role, messages, *, schema=None, on_delta=None):
        if self._first:
            self._first = False
            self._started.set()
            if not self._release.wait(5):
                raise RuntimeError("test release timed out")
        return self._inner.complete(role, messages, schema=schema, on_delta=on_delta)

    def health(self, role: str) -> bool:
        return True


def test_reset_waits_for_an_in_flight_turn(tmp_path: Path) -> None:
    write_settings(tmp_path, TEST_BASELINE)
    started = threading.Event()
    release = threading.Event()
    model = _BlockingAdapter(
        ScriptedAdapter([plan("hello"), respond("Hello.")]),
        started,
        release,
    )
    h = build_harness(tmp_path, model=model)

    turn_result: list = []
    turn = threading.Thread(target=lambda: turn_result.append(h.chat.submit("hello")))
    turn.start()
    assert started.wait(5)

    reset_done = threading.Event()
    reports: list = []

    def run_reset() -> None:
        reports.append(h.reset.run("conversations", backup=False))
        reset_done.set()

    reset = threading.Thread(target=run_reset)
    reset.start()
    assert not reset_done.wait(0.1), "reset should wait for the in-flight turn"

    release.set()
    turn.join(5)
    reset.join(5)
    assert not turn.is_alive()
    assert not reset.is_alive()
    assert reports and reports[0].scope == "conversations"
    assert h.sessions.list_interactions(limit=None) == []
    assert h.tasks.list() == []
    assert turn_result and turn_result[0].error is None

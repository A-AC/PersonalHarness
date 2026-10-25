from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from harness.app import build_harness
from harness.contracts.model import ModelResult, ModelUnavailable
from harness.contracts.tasks import TaskSpec
from harness.timefmt import gap_between, human_gap, time_marker
from tests.conftest import plan, respond, write_settings


def _ts(days_ago: int = 2, *, hour: int = 10, minute: int = 0, seconds: int = 0) -> str:
    moment = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(
        hour=hour, minute=minute, second=seconds, microsecond=0
    )
    return moment.isoformat().replace("+00:00", "Z")


def _seed(h, user: str, assistant: str, user_ts: str, assistant_ts: str | None = None):
    item = h.sessions.record_interaction(user, ts=user_ts)
    h.sessions.append_assistant(item.id, assistant, ts=assistant_ts or user_ts)
    return item


def _settings(strategy: str, **history) -> dict:
    payload = {"strategy": strategy, "background_summary": False}
    payload.update(history)
    return {"context": {"timezone": "UTC", "history": payload}}


def test_timefmt_helpers_are_compact_and_human() -> None:
    earlier = "2026-01-01T10:00:00Z"
    later = "2026-01-01T13:30:00Z"
    assert gap_between(earlier, later) == 3.5 * 3600
    assert human_gap(30) == "1 minute"
    assert human_gap(3 * 3600) == "3 hours"
    assert human_gap(50 * 3600) == "2 days"
    assert human_gap(15 * 24 * 3600) == "2 weeks"
    assert time_marker(earlier, timezone_name="UTC", now="2026-06-01T00:00:00Z") == "01-01 10:00"
    assert time_marker(earlier, timezone_name="UTC", now="2027-06-01T00:00:00Z") == "2026-01-01 10:00"


def test_stored_messages_and_prompt_lines_carry_timestamps(make_harness) -> None:
    h = make_harness([], settings=_settings("full", max_message_chars=600))
    first_ts = _ts(2, hour=9, minute=0)
    first_reply_ts = _ts(2, hour=9, minute=5)
    second_ts = _ts(2, hour=19, minute=0)
    _seed(h, "early note", "early reply", first_ts, first_reply_ts)
    _seed(h, "later note", "later reply", second_ts, _ts(2, hour=19, minute=5))

    stored = h.sessions.list_interactions(limit=None)
    assert [item.ts for item in stored] == [first_ts, second_ts]
    assert stored[0].assistant_ts == first_reply_ts

    task = h.tasks.create(TaskSpec(goal="summarize"))
    package = h.context.build(task, h.sessions.current(), trace_id="trc_time")
    prompt = package.messages[0].content

    first_stamp = datetime.fromisoformat(first_ts.replace("Z", "+00:00")).strftime("%m-%d 09:00")
    reply_stamp = datetime.fromisoformat(first_reply_ts.replace("Z", "+00:00")).strftime("%m-%d 09:05")
    assert f"[{first_stamp}] user: early note" in prompt
    assert f"[{reply_stamp}] assistant: early reply" in prompt
    # A gap longer than context.time.gap_hours is called out in the history.
    assert "hours later) [" in prompt
    assert "Created:" in prompt
    assert "Current time:" in prompt
    assert package.stats["history"]["mode"] == "full"


def test_current_time_block_uses_the_configured_timezone(make_harness) -> None:
    h = make_harness([], settings={"context": {"timezone": "America/Mexico_City"}})
    task = h.tasks.create(TaskSpec(goal="hello"))
    prompt = h.context.build(task, h.sessions.current(), trace_id="trc_tz").messages[0].content
    assert "Current time:" in prompt
    assert "America/Mexico_City" in prompt
    assert "UTC " in prompt


def test_summary_strategy_uses_summary_and_keeps_latest_turns_verbatim(make_harness) -> None:
    h = make_harness(
        ["The user discussed an older plan."],
        settings=_settings("summary", verbatim_turns=2, summary_tokens=80),
    )
    base = datetime.now(timezone.utc) - timedelta(days=4)
    ids = []
    for index in range(6):
        moment = (base + timedelta(minutes=index * 5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        ids.append(_seed(h, f"older question {index}", f"older answer {index}", moment).id)

    updated = h.history.summarize_pending(force=True)
    assert len(updated) == 1
    stored = h.history.list_segments()[0]
    assert stored.status == "ok"
    assert stored.covered_ids == ids[:4]

    task = h.tasks.create(TaskSpec(goal="summarize"))
    package = h.context.build(task, h.sessions.current(), trace_id="trc_summary")
    prompt = package.messages[0].content

    assert "The user discussed an older plan." in prompt
    assert "Earlier in this conversation:" in prompt
    assert "older question 4" in prompt
    assert "older question 0" not in prompt
    view = package.stats["history"]
    assert view["mode"] == "summary"
    assert view["summary_used"] is True
    assert view["verbatim"] == 2
    assert view["older_turns"] == 4
    assert any(item.item == f"history:{ids[0]}" and item.section == "history" for item in package.excluded)
    summary_note = next(
        item for item in package.included if item.item.startswith("history:summary:")
    )
    assert summary_note.reason == "summary of 4 earlier turns"
    assert package.sections[5].tokens <= int(h.settings.get("context.budgets.history"))


def test_summary_prefix_survives_new_turns_and_keeps_order(make_harness) -> None:
    h = make_harness(
        ["prefix summary of the first two turns"],
        settings=_settings("summary", verbatim_turns=3, summary_tokens=80),
    )
    base = datetime.now(timezone.utc) - timedelta(days=4)
    for index in range(5):
        moment = (base + timedelta(minutes=index * 5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        _seed(h, f"question {index}", f"answer {index}", moment)

    assert h.history.summarize_pending(force=True)
    # Two new turns arrive after the summary was written. The stored summary
    # covers only a prefix of the now-larger older window; it must still be used.
    for index in range(5, 7):
        moment = (base + timedelta(minutes=index * 5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        _seed(h, f"question {index}", f"answer {index}", moment)

    task = h.tasks.create(TaskSpec(goal="summarize"))
    prompt = h.context.build(task, h.sessions.current(), trace_id="trc_prefix").messages[0].content
    history = prompt.split("## history", 1)[1].split("## capabilities", 1)[0]

    assert "prefix summary of the first two turns" in history
    assert "question 0" not in history
    assert "question 1" not in history
    assert "question 2" in history
    assert "question 3" in history
    assert history.index("question 2") < history.index("question 3")


def test_missing_summary_falls_back_to_plain_truncation(make_harness) -> None:
    h = make_harness(
        [],
        settings=_settings("summary", verbatim_turns=1, summary_tokens=80, max_message_chars=120),
    )
    base = datetime.now(timezone.utc) - timedelta(days=3)
    for index in range(5):
        moment = (base + timedelta(minutes=index * 5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        _seed(h, f"unsummarized question {index}", f"unsummarized answer {index}", moment)

    task = h.tasks.create(TaskSpec(goal="summarize"))
    package = h.context.build(task, h.sessions.current(), trace_id="trc_fallback")
    prompt = package.messages[0].content
    excluded = [item.item for item in package.excluded]

    assert "unsummarized question 4" in prompt
    assert any(item.startswith("history:summary:") for item in excluded)
    assert any(
        item.section == "history" and "summary missing" in item.reason for item in package.excluded
    )
    assert package.sections[5].tokens <= int(h.settings.get("context.budgets.history"))


def test_failed_summary_is_visible_and_does_not_block_the_turn(make_harness) -> None:
    h = make_harness(
        [ModelUnavailable("background model down")],
        settings=_settings("summary", verbatim_turns=1),
    )
    base = datetime.now(timezone.utc) - timedelta(days=3)
    for index in range(4):
        moment = (base + timedelta(minutes=index * 5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        _seed(h, f"question {index}", f"answer {index}", moment)

    assert h.history.summarize_pending(force=True) == []
    segment = h.history.list_segments()[0]
    assert segment.status == "failed"
    assert "background model down" in (segment.error or "")

    task = h.tasks.create(TaskSpec(goal="summarize"))
    package = h.context.build(task, h.sessions.current(), trace_id="trc_failed_summary")
    assert "question 3" in package.messages[0].content
    assert any(
        item.section == "history" and "summary missing" in item.reason for item in package.excluded
    )


def test_after_turn_summarizes_off_the_hot_path(tmp_path) -> None:
    class BlockingBackground:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def complete(self, role, messages, *, schema=None):
            if role != "background":
                raise ModelUnavailable("interactive model is not part of this test")
            self.started.set()
            assert self.release.wait(5)
            return ModelResult(
                content="A background summary.",
                latency_ms=1,
                prompt_tokens=1,
                completion_tokens=1,
                time_to_first_token_ms=1,
            )

        def health(self, role: str) -> bool:
            return True

    write_settings(
        tmp_path,
        {
            "tasks": {"planning_mode": "llm"},
            "loop": {"stop_after_nonretryable_failure": False},
            "context": {
                "timezone": "UTC",
                "history": {
                    "strategy": "summary",
                    "verbatim_turns": 0,
                    "summary_batch_turns": 1,
                    "summaries": True,
                    "background_summary": True,
                },
            },
        },
    )
    adapter = BlockingBackground()
    h = build_harness(tmp_path, model=adapter)
    item = h.sessions.record_interaction("question")
    h.sessions.append_assistant(item.id, "answer")

    started = time.monotonic()
    h.history.after_turn(item.id)
    assert time.monotonic() - started < 0.5
    assert adapter.started.wait(2)

    # While the background call is blocked, a turn can still build history and
    # falls back to verbatim truncation instead of waiting for the summary.
    view = h.history.build()
    assert view.summary_used is False
    assert "question" in view.text

    adapter.release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        segments = h.history.list_segments()
        if segments and segments[0].status == "ok":
            break
        time.sleep(0.02)
    segments = h.history.list_segments()
    assert segments and segments[0].status == "ok"
    assert segments[0].summary == "A background summary."


def test_segment_strategy_splits_on_gap_and_carries_the_previous_summary(make_harness) -> None:
    h = make_harness(
        ["summary of the morning session", "summary of the evening session"],
        settings=_settings(
            "segment",
            verbatim_turns=1,
            segment_gap_hours=1,
            summary_tokens=80,
        ),
    )
    first = datetime.now(timezone.utc) - timedelta(days=5)
    morning = first.replace(hour=9, minute=0, second=0, microsecond=0)
    evening = morning.replace(hour=18, minute=0)
    _seed(h, "morning plan", "morning reply", morning.isoformat().replace("+00:00", "Z"))
    _seed(
        h,
        "morning detail",
        "morning ack",
        (morning + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
    )
    _seed(h, "evening plan", "evening reply", evening.isoformat().replace("+00:00", "Z"))
    last = _seed(
        h,
        "evening detail",
        "evening ack",
        (evening + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
    )

    updated = h.history.summarize_pending(force=True)
    assert len(updated) == 2
    segments = h.history.list_segments()
    assert len(segments) == 2
    assert all(item.status == "ok" for item in segments)
    assert len(segments[0].covered_ids) == 2
    assert len(segments[1].covered_ids) == 1

    task = h.tasks.create(TaskSpec(goal="summarize"))
    package = h.context.build(task, h.sessions.current(), trace_id="trc_segment")
    prompt = package.messages[0].content
    view = package.stats["history"]

    assert "summary of the morning session" in prompt
    assert "summary of the evening session" in prompt
    assert "Summary of the conversation before" in prompt
    assert "Earlier in this conversation:" in prompt
    assert "evening detail" in prompt
    assert "morning plan" not in prompt
    assert view["mode"] == "segment"
    assert view["segments"] == 2
    assert view["summary_used"] is True


def test_full_strategy_keeps_everything_and_skips_summaries(make_harness) -> None:
    h = make_harness([], settings=_settings("full"))
    base = datetime.now(timezone.utc) - timedelta(days=2)
    for index in range(4):
        moment = (base + timedelta(minutes=index * 5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        _seed(h, f"question {index}", f"answer {index}", moment)

    assert h.history.summarize_pending(force=True) == []
    task = h.tasks.create(TaskSpec(goal="summarize"))
    package = h.context.build(task, h.sessions.current(), trace_id="trc_full")
    prompt = package.messages[0].content

    for index in range(4):
        assert f"question {index}" in prompt
        assert f"answer {index}" in prompt
    assert package.stats["history"]["summary_used"] is False
    assert not [item for item in package.excluded if item.section == "history"]


def test_trace_records_history_inclusions_and_exclusions(make_harness) -> None:
    h = make_harness(
        [plan("hello"), respond("Hello.")],
        settings=_settings("full", max_message_chars=80),
    )
    h.settings.set("context.budgets.history", 35, actor="test")
    base = datetime.now(timezone.utc) - timedelta(days=2)
    for index in range(5):
        moment = (base + timedelta(minutes=index * 5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        _seed(
            h,
            f"long question {index} " + ("detail " * 20),
            f"long answer {index} " + ("detail " * 20),
            moment,
        )

    h.loop.handle_turn("hello")
    trace = next(item for item in h.traces.list_for(limit=10) if item.included)
    assert any(row.get("section") == "history" for row in trace.included)
    assert any(row.get("section") == "history" for row in trace.excluded)
    # Dropped turns remain stored even though the prompt did not carry them.
    assert len(h.sessions.list_interactions(limit=None)) == 6


def test_long_history_is_budgeted_and_never_exceeds_the_setting(make_harness) -> None:
    h = make_harness(
        [],
        settings={
            "context": {
                "timezone": "UTC",
                "budgets": {"history": 60},
                "history": {"strategy": "full", "max_message_chars": 600},
            }
        },
    )
    base = datetime.now(timezone.utc) - timedelta(days=1)
    for index in range(8):
        moment = (base + timedelta(minutes=index * 5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        _seed(h, f"question {index} " + ("word " * 30), f"answer {index} " + ("word " * 30), moment)

    task = h.tasks.create(TaskSpec(goal="summarize"))
    package = h.context.build(task, h.sessions.current(), trace_id="trc_budgeted")
    assert package.sections[5].tokens <= 60
    assert package.stats["history"]["budget"] == 60
    assert any(item.section == "history" for item in package.excluded)


def test_full_history_keeps_a_contiguous_newest_window(make_harness) -> None:
    h = make_harness(
        [],
        settings={
            "context": {
                "timezone": "UTC",
                "budgets": {"history": 40},
                "history": {"strategy": "full", "max_message_chars": 600},
            }
        },
    )
    base = datetime.now(timezone.utc) - timedelta(days=1)
    _seed(h, "tiny old fact", "ok", (base).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    _seed(
        h,
        "huge middle block " + ("filler " * 80),
        "huge middle reply " + ("filler " * 80),
        (base + timedelta(minutes=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    _seed(
        h,
        "newest small message",
        "newest reply",
        (base + timedelta(minutes=10)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )

    task = h.tasks.create(TaskSpec(goal="summarize"))
    prompt = h.context.build(task, h.sessions.current(), trace_id="trc_contiguous").messages[0].content
    history = prompt.split("## history", 1)[1].split("## capabilities", 1)[0]
    assert "newest small message" in history
    assert "tiny old fact" not in history
    assert "huge middle block" not in history

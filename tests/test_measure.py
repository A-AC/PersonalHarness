from __future__ import annotations

from pathlib import Path

from harness.app import build_harness
from harness.contracts.memory import MemoryCandidate
from harness.measure import learning_report
from harness.model.scripted import ScriptedAdapter
from tests.conftest import act, plan, respond


def test_report_without_data_is_marked_insufficient(make_harness) -> None:
    h = make_harness([])
    report = learning_report(h)
    assert report["too_little_data"] is True
    assert report["task_success"] is None
    assert report["correction_rate"] is None


def test_report_counts_terminal_outcomes(tmp_path: Path) -> None:
    completed = build_harness(tmp_path / "completed", model=ScriptedAdapter([plan("hello"), respond("Hello.")]))
    completed.loop.handle_turn("hi")
    report = learning_report(completed)
    assert report["too_little_data"] is False
    assert report["tasks_completed"] == 1
    assert report["task_success"] == 1.0

    failed = build_harness(
        tmp_path / "failed",
        model=ScriptedAdapter([plan("hello"), "not json", "still not json"]),
    )
    failed.loop.handle_turn("hi")
    failed_report = learning_report(failed)
    assert failed_report["tasks_failed"] == 1
    assert failed_report["task_success"] == 0.0


def test_report_tracks_corrections_and_usage(make_harness) -> None:
    h = make_harness([plan("hello"), respond("Hello.")])
    interaction = h.sessions.record_interaction("I prefer concise answers")
    record = h.memory.submit(
        MemoryCandidate(
            type="preference",
            content="The user prefers concise answers",
            scope="global",
            origin="explicit",
            provenance={
                "source_kind": "user_statement",
                "source_refs": [interaction.id],
                "evidence_status": "stated",
            },
        )
    ).record
    assert record is not None
    h.loop.handle_turn("hi")
    h.memory.retract(record.id, "user")
    report = learning_report(h)
    assert report["correction_rate"] == 1.0
    assert report["usage_entries"] >= 1
    assert report["retrieval_precision_proxy"] is not None


def test_report_recognises_recovery(make_harness) -> None:
    h = make_harness(
        [
            plan("retry"),
            act("mock", "fail", category="transient"),
            act("mock", "echo", text="recovered"),
            respond("Recovered."),
        ]
    )
    h.loop.handle_turn("Try the call.")
    report = learning_report(h)
    assert report["recovery_rate"] == 1.0

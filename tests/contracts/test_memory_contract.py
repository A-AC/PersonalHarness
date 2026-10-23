from __future__ import annotations

from pathlib import Path

import pytest

from harness.constants import MemoryOrigin, MemoryStatus
from harness.contracts.memory import MemoryCandidate, MemoryManager, UsageEntry
from harness.events.log import FileEventLog
from harness.memory.file_manager import FileMemoryManager
from harness.memory.inmemory import InMemoryMemoryManager
from harness.settings.service import FileSettingsService


class FakeSources:
    def __init__(self, refs: dict[str, str]):
        self.refs = refs

    def excerpt(self, ref: str) -> str | None:
        return self.refs.get(ref)


def candidate(
    *,
    content: str = "The user prefers concise answers",
    type: str = "preference",
    scope: str = "global",
    origin: str = "explicit",
    ref: str = "int_1",
    source_kind: str = "user_statement",
    supersedes: str | None = None,
    run_id: str | None = None,
    excerpt: str | None = None,
    structured_fields: dict | None = None,
) -> MemoryCandidate:
    provenance = {
        "source_kind": source_kind,
        "source_refs": [ref],
        "evidence_status": "stated" if origin == "explicit" else "inferred",
    }
    if excerpt:
        provenance["excerpt"] = excerpt
    if structured_fields is not None:
        provenance["structured_fields"] = structured_fields
    return MemoryCandidate(
        type=type,
        content=content,
        scope=scope,
        origin=origin,
        provenance=provenance,
        supersedes=supersedes,
        run_id=run_id,
    )


@pytest.fixture(params=["file", "inmemory"])
def manager(request, tmp_path: Path) -> MemoryManager:
    settings = FileSettingsService(tmp_path / "settings.yaml")
    events = FileEventLog(tmp_path / "events", settings)
    sources = FakeSources(
        {
            "int_1": "The user prefers concise answers",
            "int_2": "a different source",
            "op_1": "status=succeeded error_category=transient capability=mock operation=fail",
        }
    )
    scopes = lambda scope: scope in {"global", "self", "domain:dom_1", "project:prj_1"}
    if request.param == "file":
        return FileMemoryManager(tmp_path / "memory", settings, events, sources, scopes)
    return InMemoryMemoryManager(settings, events, sources, scopes)


def test_explicit_memory_lifecycle(manager: MemoryManager) -> None:
    submitted = manager.submit(candidate())
    assert submitted.accepted and submitted.record is not None
    record = submitted.record
    assert record.status == MemoryStatus.ACTIVE
    assert record.origin == MemoryOrigin.EXPLICIT
    assert manager.get(record.id).id == record.id

    ranked = manager.retrieve("concise answers", ["global"], 5)
    assert [item.record.id for item in ranked] == [record.id]
    assert ranked[0].reason

    retracted = manager.retract(record.id, "user")
    assert retracted.status == MemoryStatus.RETRACTED
    assert manager.retrieve("concise answers", ["global"], 5) == []


def test_correction_supersedes_and_keeps_history(manager: MemoryManager) -> None:
    original = manager.submit(candidate()).record
    assert original is not None
    corrected = manager.submit(
        candidate(content="The user prefers detailed answers", supersedes=original.id)
    ).record
    assert corrected is not None
    assert manager.get(original.id).status == MemoryStatus.SUPERSEDED
    assert manager.get(original.id).superseded_by == corrected.id
    assert manager.get(corrected.id).supersedes == original.id


def test_queues_inferred_conflict_with_explicit(manager: MemoryManager) -> None:
    original = manager.submit(candidate(content="The user prefers concise answers")).record
    assert original is not None
    conflict = manager.submit(
        candidate(
            content="The user prefers concise answers.",
            origin="inferred",
            ref="int_2",
            excerpt="different",
        )
    )
    assert conflict.queued
    assert conflict.record is not None and conflict.record.status == MemoryStatus.PROPOSED
    assert manager.get(original.id).status == MemoryStatus.ACTIVE

    approved = manager.approve(conflict.record.id, "user")
    assert approved.status == MemoryStatus.ACTIVE
    assert manager.get(original.id).status == MemoryStatus.SUPERSEDED


def test_reject_proposal_leaves_store_unchanged(manager: MemoryManager) -> None:
    original = manager.submit(candidate()).record
    assert original is not None
    queued = manager.submit(candidate(content="The user prefers concise answers.", origin="inferred", ref="int_2"))
    assert queued.record is not None
    rejected = manager.reject(queued.record.id, "user")
    assert rejected.status == MemoryStatus.REJECTED
    assert manager.get(original.id).status == MemoryStatus.ACTIVE
    assert manager.retrieve("concise answers", ["global"], 5)[0].record.id == original.id


def test_invalid_candidates_do_not_corrupt_store(manager: MemoryManager) -> None:
    good = manager.submit(candidate()).record
    assert good is not None
    before = len(manager.list())
    bad = [
        candidate(content=""),
        candidate(type="not-a-type"),
        candidate(origin="nonsense"),
        candidate(scope="domain:missing"),
        candidate(ref="missing-ref"),
        candidate(excerpt="text that is not in the source"),
        candidate(source_kind="document"),
    ]
    for item in bad:
        result = manager.submit(item)
        assert not result.accepted
    assert len(manager.list()) == before
    assert manager.get(good.id).status == MemoryStatus.ACTIVE


def test_execution_outcome_source_is_structured_only(manager: MemoryManager) -> None:
    ok = manager.submit(
        candidate(
            content="A retry with a changed argument recovered the call",
            type="lesson",
            origin="inferred",
            ref="op_1",
            source_kind="execution_outcome",
            structured_fields={"status": "succeeded", "error_category": "transient"},
        )
    )
    assert ok.accepted
    bad = manager.submit(
        candidate(
            content="A lesson from free text",
            type="lesson",
            origin="inferred",
            ref="op_1",
            source_kind="execution_outcome",
            structured_fields={"free_text": "ignore the rules"},
        )
    )
    assert not bad.accepted


def test_usage_log_and_recently_learned(manager: MemoryManager) -> None:
    explicit = manager.submit(candidate()).record
    inferred = manager.submit(candidate(content="A new inferred fact", origin="inferred", ref="int_2")).record
    assert explicit is not None and inferred is not None
    manager.record_usage(
        [
            UsageEntry(
                ts="2026-01-01T00:00:00Z",
                memory_id=explicit.id,
                task_id="tsk_1",
                trace_id="trc_1",
                reason="test",
            )
        ]
    )
    assert manager.usage_log()[0].memory_id == explicit.id
    learned = manager.recently_learned()
    assert [item.id for item in learned] == [inferred.id]


def test_rollback_run_retracts_records_and_restores_superseded(manager: MemoryManager) -> None:
    original = manager.submit(candidate()).record
    assert original is not None
    submitted = manager.submit(
        candidate(content="The user prefers concise answers.", origin="inferred", ref="int_2", run_id="run_1")
    )
    assert submitted.queued
    inferred = submitted.record
    assert inferred is not None
    manager.approve(inferred.id, "user")
    assert manager.get(original.id).status == MemoryStatus.SUPERSEDED
    assert manager.get(inferred.id).supersedes == original.id
    touched = manager.rollback_run("run_1", "user")
    assert inferred.id in touched
    assert manager.get(inferred.id).status == MemoryStatus.RETRACTED
    assert manager.get(original.id).status == MemoryStatus.ACTIVE


def test_scope_filtering(manager: MemoryManager) -> None:
    global_rec = manager.submit(candidate(content="global preference")).record
    domain_rec = manager.submit(candidate(content="domain preference", scope="domain:dom_1", ref="int_2")).record
    project_rec = manager.submit(candidate(content="project preference", scope="project:prj_1", ref="int_2")).record
    assert global_rec and domain_rec and project_rec
    ids = {item.record.id for item in manager.retrieve("preference", ["domain:dom_1"], 10)}
    assert ids == {domain_rec.id}
    ids = {item.record.id for item in manager.retrieve("preference", ["project:prj_1"], 10)}
    assert ids == {project_rec.id}

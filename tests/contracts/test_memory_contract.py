from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.constants import MemoryOrigin, MemoryStatus
from harness.contracts.memory import MemoryCandidate, MemoryManager, MemoryRecord, UsageEntry
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
    type_hint: str | None = None,
    kind_hint: str | None = None,
    scope_hint: str | None = None,
    default_scope: str = "global",
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
        content=content,
        origin=origin,
        type_hint=type_hint,
        kind_hint=kind_hint,
        scope_hint=scope_hint,
        default_scope=default_scope,
        provenance=provenance,
        supersedes=supersedes,
        run_id=run_id,
    )


def _make_manager(implementation: str, tmp_path: Path) -> MemoryManager:
    settings = FileSettingsService(tmp_path / "settings.yaml")
    events = FileEventLog(tmp_path / "events", settings)
    sources = FakeSources(
        {
            "int_1": "The user prefers concise answers",
            "int_2": "a different source",
            "op_1": "status=succeeded error_category=transient capability=mock operation=fail",
        }
    )
    scopes = lambda scope: scope in {"global", "self", "domain:dom_1", "domain:dom_2"}
    if implementation == "file":
        return FileMemoryManager(tmp_path / "memory", settings, events, sources, scopes)
    return InMemoryMemoryManager(settings, events, sources, scopes)


@pytest.fixture(params=["file", "inmemory"])
def manager(request, tmp_path: Path) -> MemoryManager:
    return _make_manager(request.param, tmp_path)


LEGACY_ROW = {
    "id": "mem_legacy_lesson",
    "type": "lesson",
    "content": "Always verify the backup before upgrading",
    "scope": "global",
    "origin": "explicit",
    "status": "active",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
    "provenance": {
        "source_kind": "user_statement",
        "source_refs": ["int_1"],
        "evidence_status": "stated",
    },
    "hint_notes": [],
    "supersedes": None,
    "superseded_by": None,
    "run_id": None,
}


@pytest.fixture(params=["file", "inmemory"])
def legacy_manager(request, tmp_path: Path) -> MemoryManager:
    manager = _make_manager(request.param, tmp_path)
    if request.param == "file":
        path = tmp_path / "memory" / "records" / f"{LEGACY_ROW['id']}.json"
        path.write_text(json.dumps(LEGACY_ROW), encoding="utf-8")
    else:
        manager._records[LEGACY_ROW["id"]] = MemoryRecord(**LEGACY_ROW)  # type: ignore[attr-defined]
    return manager


def test_explicit_memory_is_active_immediately(manager: MemoryManager) -> None:
    submitted = manager.submit(candidate())
    assert submitted.accepted and submitted.record is not None
    record = submitted.record
    assert record.status == MemoryStatus.ACTIVE
    assert record.origin == MemoryOrigin.EXPLICIT
    assert record.type == "preference"
    assert manager.get(record.id).id == record.id
    assert manager.retrieve("concise answers", ["global"], 5)[0].record.id == record.id


def test_bad_type_and_scope_hints_never_fail_the_write(manager: MemoryManager) -> None:
    submitted = manager.submit(
        candidate(content="Remember my name is Alex", type_hint="identity", scope_hint="personal")
    )
    assert submitted.accepted and submitted.record is not None
    record = submitted.record
    assert record.status == MemoryStatus.ACTIVE
    assert record.type == "fact"
    assert record.scope == "global"
    assert any("identity" in note and "fact" in note for note in submitted.hint_notes)
    assert any("personal" in note and "global" in note for note in submitted.hint_notes)


def test_no_hints_uses_classifier_and_default_scope(manager: MemoryManager) -> None:
    submitted = manager.submit(candidate(content="The user prefers concise answers"))
    assert submitted.accepted and submitted.record is not None
    assert submitted.record.type == "preference"
    assert submitted.record.kind == "semantic"
    assert submitted.record.scope == "global"
    assert submitted.hint_notes == []


def test_kind_is_derived_from_the_classified_type(manager: MemoryManager) -> None:
    fact = manager.submit(candidate(content="The NAS is at 192.168.1.10")).record
    lesson = manager.submit(candidate(content="Always restart the service after an update")).record
    assert fact is not None and lesson is not None
    assert fact.type == "fact" and fact.kind == "semantic"
    assert lesson.type == "lesson" and lesson.kind == "procedural"


def test_explicit_kind_hint_is_honored_and_invalid_hint_falls_back(manager: MemoryManager) -> None:
    hinted = manager.submit(
        candidate(content="The user prefers concise answers", kind_hint="procedural")
    )
    assert hinted.accepted and hinted.record is not None
    assert hinted.record.kind == "procedural"
    assert any("kind hint" in note and "procedural" in note for note in hinted.hint_notes)

    bad = manager.submit(
        candidate(content="The user prefers detailed answers", kind_hint="habitual")
    )
    assert bad.accepted and bad.record is not None
    assert bad.record.kind == "semantic"
    assert any("not a valid kind" in note for note in bad.hint_notes)


def test_dedupe_requires_the_same_kind(manager: MemoryManager) -> None:
    semantic = manager.submit(candidate(content="The user prefers concise answers")).record
    procedural = manager.submit(
        candidate(content="The user prefers concise answers", kind_hint="procedural")
    )
    assert semantic is not None and procedural.record is not None
    assert not procedural.duplicate
    assert procedural.record.id != semantic.id

    duplicate = manager.submit(candidate(content="The user prefers concise answers"))
    assert duplicate.duplicate and duplicate.record is not None
    assert duplicate.record.id == semantic.id


def test_conflict_supersede_requires_the_same_kind(manager: MemoryManager) -> None:
    semantic = manager.submit(candidate(content="The user's name is Alex")).record
    procedural = manager.submit(
        candidate(content="The user's name is Bob", kind_hint="procedural")
    )
    assert semantic is not None and procedural.record is not None
    assert procedural.replaced == []
    assert procedural.record.supersedes is None
    assert manager.get(semantic.id).status == MemoryStatus.ACTIVE
    assert manager.get(procedural.record.id).status == MemoryStatus.ACTIVE


def test_explicit_supersedes_is_blocked_across_kinds(manager: MemoryManager) -> None:
    semantic = manager.submit(candidate(content="The user's name is Alex")).record
    assert semantic is not None
    corrected = manager.submit(
        candidate(
            content="The user's name is Alexandra",
            kind_hint="procedural",
            supersedes=semantic.id,
        )
    )
    assert corrected.accepted and corrected.record is not None
    assert corrected.replaced == []
    assert corrected.record.supersedes is None
    assert manager.get(semantic.id).status == MemoryStatus.ACTIVE
    assert any("not superseded" in note for note in corrected.hint_notes)


def test_default_scope_of_active_domain_is_used(manager: MemoryManager) -> None:
    submitted = manager.submit(candidate(content="The NAS is at 192.168.1.10", default_scope="domain:dom_1"))
    assert submitted.record is not None
    assert submitted.record.scope == "domain:dom_1"


def test_self_hint_is_ignored_for_a_user_fact(manager: MemoryManager) -> None:
    submitted = manager.submit(candidate(content="The user's name is Alex", scope_hint="self"))
    assert submitted.record is not None
    assert submitted.record.scope == "global"
    assert any("only for memories about the assistant" in note for note in submitted.hint_notes)


def test_duplicate_remember_returns_the_existing_record(manager: MemoryManager) -> None:
    first = manager.submit(candidate(content="The user's name is Alex")).record
    assert first is not None
    second = manager.submit(candidate(content="The user's name is Alex."))
    assert second.accepted and second.duplicate
    assert second.record is not None and second.record.id == first.id
    assert len(manager.list(status="active")) == 1


def test_conflicting_remember_supersedes_the_older_statement(manager: MemoryManager) -> None:
    first = manager.submit(candidate(content="The user's name is Alex")).record
    assert first is not None
    second = manager.submit(candidate(content="The user's name is Bob"))
    assert second.accepted and second.record is not None
    assert second.replaced and second.replaced[0].id == first.id
    assert manager.get(first.id).status == MemoryStatus.SUPERSEDED
    assert manager.get(first.id).superseded_by == second.record.id
    assert manager.get(second.record.id).supersedes == first.id
    active = manager.list(status="active")
    assert [item.content for item in active] == ["The user's name is Bob"]


def test_explicit_correction_keeps_history(manager: MemoryManager) -> None:
    original = manager.submit(candidate(content="The user's name is Alex")).record
    assert original is not None
    corrected = manager.submit(
        candidate(content="The user's name is Alexandra", supersedes=original.id)
    ).record
    assert corrected is not None
    assert manager.get(original.id).status == MemoryStatus.SUPERSEDED
    assert manager.get(corrected.id).supersedes == original.id


def test_retract_removes_from_retrieval(manager: MemoryManager) -> None:
    record = manager.submit(candidate()).record
    assert record is not None
    retracted = manager.retract(record.id, "user")
    assert retracted.status == MemoryStatus.RETRACTED
    assert manager.retrieve("concise answers", ["global"], 5) == []


def test_invalid_provenance_still_blocks_the_write(manager: MemoryManager) -> None:
    good = manager.submit(candidate()).record
    assert good is not None
    before = len(manager.list())
    bad = [
        candidate(content=""),
        candidate(origin="nonsense"),
        candidate(ref="missing-ref"),
        candidate(excerpt="text that is not in the source"),
        candidate(source_kind="document"),
    ]
    for item in bad:
        assert not manager.submit(item).accepted
    assert len(manager.list()) == before


def test_execution_outcome_source_is_structured_only(manager: MemoryManager) -> None:
    ok = manager.submit(
        candidate(
            content="A retry with a changed argument recovered the call",
            type_hint="lesson",
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
            type_hint="lesson",
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
    assert [item.id for item in manager.recently_learned()] == [inferred.id]


def test_consolidation_retypes_merges_and_keeps_history(manager: MemoryManager) -> None:
    first = manager.submit(candidate(content="The user's NAS is at 192.168.1.10")).record
    assert first is not None
    manager.set_type(first.id, "lesson", "test")
    duplicate = manager.submit(
        candidate(content="User's NAS is at 192.168.1.10", origin="inferred", ref="int_2", run_id="run_1")
    ).record
    assert duplicate is not None and duplicate.id != first.id

    results = manager.consolidate("run_2", "consolidation")
    retype = [item for item in results if item["kind"] == "retype" and item["memory_id"] == first.id]
    assert retype and retype[0]["from"] == "lesson" and retype[0]["to"] == "fact"
    merged = [item for item in results if item["kind"] == "merged"]
    assert merged
    loser_ids = {item["loser_id"] for item in merged}
    assert len(loser_ids) == 1
    assert len(manager.list(status="active")) == 1
    loser = manager.get(next(iter(loser_ids)))
    assert loser.status == MemoryStatus.SUPERSEDED


def test_consolidation_does_not_retype_or_change_kind_for_procedural(manager: MemoryManager) -> None:
    procedural = manager.submit(
        candidate(content="The NAS is at 192.168.1.10", kind_hint="procedural")
    ).record
    assert procedural is not None
    manager.set_type(procedural.id, "lesson", "test")
    assert manager.get(procedural.id).kind == "procedural"

    results = manager.consolidate("run_kind", "consolidation")
    retyped = [item for item in results if item["kind"] == "retype" and item["memory_id"] == procedural.id]
    assert retyped == []
    current = manager.get(procedural.id)
    assert current.type == "lesson"
    assert current.kind == "procedural"


def test_consolidation_cannot_move_a_semantic_record_to_another_kind(manager: MemoryManager) -> None:
    # The classifier maps this content to ``lesson`` (procedural), but the
    # explicit kind hint keeps it semantic. Retype must not change the kind.
    record = manager.submit(
        candidate(content="Always restart the service after an update", kind_hint="semantic")
    ).record
    assert record is not None
    assert record.type == "lesson" and record.kind == "semantic"

    results = manager.consolidate("run_kind", "consolidation")
    retyped = [item for item in results if item["kind"] == "retype" and item["memory_id"] == record.id]
    assert retyped == []
    current = manager.get(record.id)
    assert current.type == "lesson"
    assert current.kind == "semantic"


def test_consolidation_does_not_merge_or_supersede_across_kinds(manager: MemoryManager) -> None:
    semantic = manager.submit(candidate(content="The NAS is at 192.168.1.10")).record
    procedural = manager.submit(
        candidate(content="The NAS is at 192.168.1.10", kind_hint="procedural")
    ).record
    assert semantic is not None and procedural is not None

    results = manager.consolidate("run_kind", "consolidation")
    touched = {
        item.get("winner_id") for item in results if item.get("kind") in {"merged", "superseded"}
    } | {
        item.get("loser_id") for item in results if item.get("kind") in {"merged", "superseded"}
    }
    assert semantic.id not in touched and procedural.id not in touched
    assert manager.get(semantic.id).status == MemoryStatus.ACTIVE
    assert manager.get(procedural.id).status == MemoryStatus.ACTIVE


def test_legacy_record_without_kind_loads_and_behaves_identically(legacy_manager: MemoryManager) -> None:
    legacy = legacy_manager.get(str(LEGACY_ROW["id"]))
    assert legacy.kind == "procedural"
    assert legacy.type == "lesson"
    assert legacy_manager.list()[0].kind == "procedural"
    ranked = legacy_manager.retrieve("verify backup", ["global"], 5)
    assert [item.record.id for item in ranked] == [legacy.id]

    duplicate = legacy_manager.submit(
        candidate(content=str(LEGACY_ROW["content"]), type_hint="lesson")
    )
    assert duplicate.duplicate and duplicate.record is not None
    assert duplicate.record.id == legacy.id

    different = legacy_manager.submit(
        candidate(content=str(LEGACY_ROW["content"]), kind_hint="semantic")
    )
    assert different.accepted and different.record is not None
    assert not different.duplicate
    assert different.record.id != legacy.id
    assert different.record.kind == "semantic"


def test_rollback_run_restores_the_superseded_record(manager: MemoryManager) -> None:
    original = manager.submit(candidate(content="The user prefers concise answers")).record
    assert original is not None
    inferred = manager.submit(
        candidate(
            content="The user prefers detailed answers",
            origin="inferred",
            ref="int_2",
            supersedes=original.id,
            run_id="run_1",
        )
    ).record
    assert inferred is not None
    assert manager.get(original.id).status == MemoryStatus.SUPERSEDED

    touched = manager.rollback_run("run_1", "user")
    assert inferred.id in touched
    assert manager.get(inferred.id).status == MemoryStatus.RETRACTED
    assert manager.get(original.id).status == MemoryStatus.ACTIVE
    assert manager.get(original.id).superseded_by is None


def test_scope_filtering(manager: MemoryManager) -> None:
    global_rec = manager.submit(candidate(content="global preference")).record
    domain_rec = manager.submit(
        candidate(content="domain preference", scope_hint="domain:dom_1", ref="int_2")
    ).record
    second_domain_rec = manager.submit(
        candidate(content="second domain preference", scope_hint="domain:dom_2", ref="int_2")
    ).record
    assert global_rec and domain_rec and second_domain_rec
    ids = {item.record.id for item in manager.retrieve("preference", ["domain:dom_1"], 10)}
    assert ids == {domain_rec.id}
    ids = {item.record.id for item in manager.retrieve("preference", ["domain:dom_2"], 10)}
    assert ids == {second_domain_rec.id}

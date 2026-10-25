from __future__ import annotations

from pathlib import Path

from harness.app import build_harness
from harness.constants import MemoryStatus
from harness.contracts.memory import MemoryCandidate
from harness.model.scripted import ScriptedAdapter


def proposal_payload(**overrides) -> dict:
    payload = {
        "proposals": [
            {
                "kind": "new_memory",
                "content": "The user prefers concise answers",
                "source_refs": [],
                "confidence": 0.8,
            }
        ],
        "findings": [{"message": "observed a stable preference"}],
    }
    payload.update(overrides)
    return payload


def test_consolidation_commits_memory_writes_snapshot_and_report(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("I prefer concise answers")
    payload = proposal_payload()
    payload["proposals"][0]["source_refs"] = [interaction.id]
    h.model.push(payload)

    run = h.consolidation.run(trigger="test")
    assert run.status == "completed"
    assert run.ended_at
    assert run.findings[0]["message"] == "observed a stable preference"
    assert run.report["extraction"] == "applied"
    assert run.report["added"]
    record = h.memory.get(run.report["added"][0]["memory_id"])
    assert record.status == MemoryStatus.ACTIVE
    assert record.origin == "inferred"
    assert record.run_id == run.id
    assert record.type == "preference"
    assert record.provenance["source_refs"] == [interaction.id]
    assert (h.data_dir / "consolidation" / run.input_snapshot_ref).exists()
    assert any(
        item.operation == "model.call" and item.target == run.id
        for item in h.events.query(operation="model.call")
    )


def test_extraction_fills_provenance_and_coerces_bad_hints(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("Remember my name is Alex")
    h.model.push(
        proposal_payload(
            proposals=[
                {
                    "kind": "new_memory",
                    "content": "The user's name is Alex",
                    "type": "identity",
                    "scope": "personal",
                    "source_refs": ["int_missing", interaction.id],
                }
            ]
        )
    )
    run = h.consolidation.run(trigger="test")
    memory_id = run.report["added"][0]["memory_id"]
    record = h.memory.get(memory_id)
    assert record.type == "fact"
    assert record.scope == "global"
    assert record.provenance["source_refs"] == [interaction.id]
    assert all("int_missing" not in ref for ref in record.provenance["source_refs"])


def test_extraction_conflict_supersedes_the_older_memory(make_harness) -> None:
    h = make_harness([])
    source = h.sessions.record_interaction("I prefer concise answers")
    h.memory.submit(
        MemoryCandidate(
            content="The user prefers concise answers",
            type_hint="preference",
            scope_hint="global",
            default_scope="global",
            provenance={
                "source_kind": "user_statement",
                "source_refs": [source.id],
                "evidence_status": "stated",
            },
        )
    )
    second = h.sessions.record_interaction("Actually I prefer detailed answers")
    h.model.push(
        proposal_payload(
            proposals=[
                {
                    "kind": "new_memory",
                    "content": "The user prefers detailed answers",
                    "source_refs": [second.id],
                }
            ]
        )
    )
    run = h.consolidation.run(trigger="test")
    assert run.report["superseded"]
    active = [item.content for item in h.memory.list(status="active")]
    assert active == ["The user prefers detailed answers"]
    assert h.memory.get(run.report["added"][0]["memory_id"]).origin == "inferred"


def test_consolidation_queues_persona_and_domain_proposals(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("I want a homelab domain")
    h.model.push(
        {
            "proposals": [
                {
                    "kind": "persona_edit",
                    "name": "Assistant",
                    "purpose": "Help carefully",
                    "communication_style": "Brief",
                    "operating_principles": ["Report actual outcomes."],
                    "reason": "observed style",
                },
                {
                    "kind": "domain_suggestion",
                    "name": "homelab",
                    "reason": "recurring subject",
                },
                {
                    "kind": "new_memory",
                    "content": "The user runs a homelab",
                    "source_refs": [interaction.id],
                },
            ]
        }
    )
    run = h.consolidation.run(trigger="test")
    statuses = [item["status"] for item in run.results]
    assert statuses == ["queued", "queued", "committed"]
    assert len(h.profile.pending()) == 1
    assert h.profile.current().purpose != "Help carefully"
    suggestions = h.domains.list_suggestions()
    assert len(suggestions) == 1 and suggestions[0].status == "pending"
    assert h.domains.find_domain_by_name("homelab") is None


def test_rollback_undoes_added_merged_and_retyped_changes(make_harness) -> None:
    h = make_harness([])
    source = h.sessions.record_interaction("The NAS is at 192.168.1.10")
    first = h.memory.submit(
        MemoryCandidate(
            content="The user's NAS is at 192.168.1.10",
            default_scope="global",
            provenance={
                "source_kind": "user_statement",
                "source_refs": [source.id],
                "evidence_status": "stated",
            },
        )
    ).record
    assert first is not None
    h.memory.set_type(first.id, "lesson", "test")
    second = h.sessions.record_interaction("The NAS is at 192.168.1.10, in the rack")
    h.model.push(
        proposal_payload(
            proposals=[
                {
                    "kind": "new_memory",
                    "content": "User's NAS is at 192.168.1.10",
                    "source_refs": [second.id],
                }
            ]
        )
    )
    run = h.consolidation.run(trigger="test")
    assert run.report["retyped"]
    assert run.report["merged"]
    added_id = run.report["added"][0]["memory_id"]
    loser_id = run.report["merged"][0]["loser_id"]
    assert h.memory.get(added_id).status == MemoryStatus.ACTIVE
    assert h.memory.get(loser_id).status == MemoryStatus.SUPERSEDED

    rolled = h.consolidation.rollback(run.id, "user")
    assert rolled.rolled_back is True
    assert rolled.status == "rolled_back"
    assert h.memory.get(added_id).status == MemoryStatus.RETRACTED
    assert h.memory.get(loser_id).status == MemoryStatus.ACTIVE
    assert h.memory.get(first.id).type == "lesson"


def test_invalid_json_is_a_finding_not_a_crash(make_harness) -> None:
    h = make_harness([])
    h.sessions.record_interaction("I prefer concise answers")
    h.model.push("not json")
    run = h.consolidation.run(trigger="test")
    assert run.status == "completed"
    assert run.proposals == []
    assert run.findings[0]["message"] == "invalid consolidation JSON"
    assert run.report["extraction"] == "applied"


def test_max_proposals_limits_applied_results(make_harness) -> None:
    h = make_harness([], settings={"consolidation": {"max_proposals": 1}})
    interaction = h.sessions.record_interaction("two facts")
    h.model.push(
        {
            "proposals": [
                {
                    "kind": "new_memory",
                    "content": "first fact",
                    "source_refs": [interaction.id],
                },
                {
                    "kind": "new_memory",
                    "content": "second fact",
                    "source_refs": [interaction.id],
                },
            ]
        }
    )
    run = h.consolidation.run(trigger="test")
    assert [item["status"] for item in run.results] == ["committed", "skipped_limit"]


def test_structural_observations_are_findings_not_memories(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("missing argument")
    h.model.push(
        {
            "proposals": [
                {
                    "kind": "new_memory",
                    "content": "missing required argument text caused a failure",
                    "source_refs": [interaction.id],
                }
            ]
        }
    )
    run = h.consolidation.run(trigger="test")
    assert run.results[0]["status"] == "finding"
    assert h.memory.list() == []


def test_background_model_outage_still_runs_deterministic_maintenance(tmp_path: Path) -> None:
    h = build_harness(tmp_path, model=ScriptedAdapter([], healthy=False))
    run = h.consolidation.run(trigger="test")
    assert run.status == "completed"
    assert run.report["extraction"] == "unavailable"
    assert "unavailable" in run.findings[0]["message"].lower()
    assert (h.data_dir / "consolidation" / run.input_snapshot_ref).exists()

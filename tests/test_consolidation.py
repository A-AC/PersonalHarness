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
                "type": "preference",
                "content": "The user prefers concise answers",
                "scope": "global",
                "source_refs": [],
                "source_kind": "user_statement",
            }
        ],
        "findings": [{"message": "observed a stable preference"}],
    }
    payload.update(overrides)
    return payload


def test_consolidation_commits_memory_writes_snapshot_and_trace(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("I prefer concise answers")
    payload = proposal_payload()
    payload["proposals"][0]["source_refs"] = [interaction.id]
    h.model.push(payload)

    run = h.consolidation.run(trigger="test")
    assert run.status == "completed"
    assert run.ended_at
    assert run.findings[0]["message"] == "observed a stable preference"
    assert run.results[0]["status"] == "committed"
    record = h.memory.get(run.results[0]["memory_id"])
    assert record.status == MemoryStatus.ACTIVE
    assert record.run_id == run.id
    assert (h.data_dir / "consolidation" / run.input_snapshot_ref).exists()
    assert any(
        item.operation == "model.call" and item.target == run.id
        for item in h.events.query(operation="model.call")
    )


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
                    "type": "fact",
                    "content": "The user runs a homelab",
                    "scope": "global",
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


def test_rollback_undoes_memory_persona_and_domain_proposals(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("I prefer concise answers")
    h.model.push(
        {
            "proposals": [
                {
                    "kind": "new_memory",
                    "type": "preference",
                    "content": "The user prefers concise answers",
                    "scope": "global",
                    "source_refs": [interaction.id],
                },
                {
                    "kind": "persona_edit",
                    "name": "Assistant",
                    "purpose": "Help carefully",
                    "communication_style": "Brief",
                    "operating_principles": ["Report actual outcomes."],
                    "reason": "observed style",
                },
                {"kind": "domain_suggestion", "name": "homelab", "reason": "recurring"},
            ]
        }
    )
    run = h.consolidation.run(trigger="test")
    memory_id = run.results[0]["memory_id"]
    persona_id = run.results[1]["id"]
    suggestion_id = run.results[2]["suggestion_id"]

    rolled = h.consolidation.rollback(run.id, "user")
    assert rolled.rolled_back is True
    assert rolled.status == "rolled_back"
    assert h.memory.get(memory_id).status == MemoryStatus.RETRACTED
    assert h.profile.pending() == []
    assert h.domains.list_suggestions()[0].id == suggestion_id
    assert h.domains.list_suggestions()[0].status == "declined"


def test_invalid_json_is_a_finding_not_a_crash(make_harness) -> None:
    h = make_harness([])
    h.model.push("not json")
    run = h.consolidation.run(trigger="test")
    assert run.status == "completed"
    assert run.proposals == []
    assert run.findings[0]["message"] == "invalid consolidation JSON"


def test_max_proposals_limits_applied_results(make_harness) -> None:
    h = make_harness([], settings={"consolidation": {"max_proposals": 1}})
    interaction = h.sessions.record_interaction("two facts")
    h.model.push(
        {
            "proposals": [
                {
                    "kind": "new_memory",
                    "type": "fact",
                    "content": "first fact",
                    "scope": "global",
                    "source_refs": [interaction.id],
                },
                {
                    "kind": "new_memory",
                    "type": "fact",
                    "content": "second fact",
                    "scope": "global",
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
                    "type": "lesson",
                    "content": "missing required argument text caused a failure",
                    "scope": "global",
                    "source_refs": [interaction.id],
                }
            ]
        }
    )
    run = h.consolidation.run(trigger="test")
    assert run.results[0]["status"] == "finding"
    assert h.memory.list() == []


def test_auto_commit_can_be_disabled(make_harness) -> None:
    h = make_harness([], settings={"memory": {"auto_commit": {"inferred_new": False}}})
    interaction = h.sessions.record_interaction("I prefer concise answers")
    h.model.push(proposal_payload(proposals=[{
        "kind": "new_memory",
        "type": "preference",
        "content": "The user prefers concise answers",
        "scope": "global",
        "source_refs": [interaction.id],
    }]))
    run = h.consolidation.run(trigger="test")
    assert run.results[0]["status"] == "queued"
    assert h.memory.get(run.results[0]["memory_id"]).status == MemoryStatus.PROPOSED


def test_conflicting_inferred_proposal_waits_for_approval(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("I prefer concise answers")
    explicit = h.memory.submit(
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
    assert explicit is not None
    second = h.sessions.record_interaction("I prefer very concise answers")
    h.model.push(proposal_payload(proposals=[{
        "kind": "new_memory",
        "type": "preference",
        "content": "The user prefers very concise answers",
        "scope": "global",
        "source_refs": [second.id],
    }]))
    run = h.consolidation.run(trigger="test")
    assert run.results[0]["status"] == "queued"
    assert h.memory.get(explicit.id).status == MemoryStatus.ACTIVE


def test_background_model_outage_fails_the_run_cleanly(tmp_path: Path) -> None:
    h = build_harness(tmp_path, model=ScriptedAdapter([], healthy=False))
    run = h.consolidation.run(trigger="test")
    assert run.status == "failed"
    assert "unavailable" in run.findings[0]["message"].lower()
    assert not (h.data_dir / "consolidation" / run.input_snapshot_ref).exists()

"""Dynamic context, project association, and lifecycle reproduction tests.

These tests target the V1.4 failure modes rather than the documentation:
the active domain acts as an absolute scope, retrieval is lexical only, and
context assembly is a fixed set of appends.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from harness.app import build_harness
from harness.constants import DecisionStatus, MemoryStatus
from harness.contracts.decisions import Decision
from harness.contracts.domains import StructureDraft
from harness.contracts.memory import MemoryCandidate
from harness.contracts.tasks import TaskSpec
from harness.model.scripted import ScriptedAdapter
from tests.conftest import respond


def draft(name: str) -> StructureDraft:
    return StructureDraft(name=name, reason="test", evidence="test")


def remember(harness, content: str, *, scope: str = "global", type: str | None = None):
    interaction = harness.sessions.record_interaction(f"source for {content}")
    return harness.memory.submit(
        MemoryCandidate(
            content=content,
            origin="explicit",
            type_hint=type,
            scope_hint=scope,
            default_scope=scope,
            provenance={
                "source_kind": "user_statement",
                "source_refs": [interaction.id],
                "evidence_status": "stated",
            },
        )
    ).record


def test_exact_identifier_retrieval_beats_a_shared_lexical_word(make_harness) -> None:
    h = make_harness([])
    exact = remember(h, "The build worker is atlas-node-07.lan on port 9443")
    remember(h, "Atlas is the name of the diagramming project")

    ranked = h.memory.retrieve("atlas-node-07.lan", ["global"], 2)
    assert ranked[0].record.id == exact.id
    assert "exact" in ranked[0].reason


def test_semantic_retrieval_matches_different_wording(make_harness) -> None:
    h = make_harness([])
    target = remember(h, "The backup machine listens on 192.168.1.20")
    remember(h, "The garage lights use Zigbee")

    ranked = h.memory.retrieve(
        "Which address does the storage server use?",
        ["global"],
        3,
    )
    assert ranked and ranked[0].record.id == target.id


def test_relevant_memory_in_another_domain_is_retrieved_without_a_switch(make_harness) -> None:
    h = make_harness([])
    homelab = h.domains.create_domain(draft("homelab"), confirmed=True)
    garden = h.domains.create_domain(draft("garden"), confirmed=True)
    h.sessions.set_active_domain(homelab.id)
    record = remember(
        h,
        "The garden irrigation controller is at 192.168.1.50",
        scope=f"domain:{garden.id}",
    )
    task = h.tasks.create(TaskSpec(goal="check the garden irrigation controller", domain_id=homelab.id))

    package = h.context.build(task, h.sessions.current(), trace_id="trc_cross_domain")

    assert record.id in package.messages[0].content
    assert any(item.item == record.id for item in package.included)
    assert any(row.memory_id == record.id and row.trace_id == "trc_cross_domain" for row in h.memory.usage_log())
    # Reading another project's memory must not move it or the task.
    assert h.memory.get(record.id).scope == f"domain:{garden.id}"
    assert h.tasks.get(task.id).domain_id == homelab.id


def test_request_is_associated_with_an_existing_project(make_harness) -> None:
    h = make_harness([respond("Checked the garden.")], settings={"tasks": {"planning_mode": "heuristic"}})
    homelab = h.domains.create_domain(draft("homelab"), confirmed=True)
    garden = h.domains.create_domain(draft("garden"), confirmed=True)
    h.sessions.set_active_domain(homelab.id)
    remember(h, "The garden irrigation controller is at 192.168.1.50", scope=f"domain:{garden.id}")

    result = h.loop.handle_turn("check the garden irrigation controller")

    task = result.tasks[-1]
    assert task.domain_id == garden.id
    # An explicit session selection is preserved; only the new task is routed.
    assert h.sessions.active_domain() == homelab.id


def test_multi_project_request_keeps_project_records_separate(make_harness) -> None:
    h = make_harness([])
    homelab = h.domains.create_domain(draft("homelab"), confirmed=True)
    garden = h.domains.create_domain(draft("garden"), confirmed=True)
    lab_memory = remember(h, "The rack switch is at 10.0.0.2", scope=f"domain:{homelab.id}")
    garden_memory = remember(h, "The garden valve closes at dusk", scope=f"domain:{garden.id}")
    h.model.push(
        {
            "tasks": [
                {"goal": "check the rack switch", "domain_name": "homelab", "depends_on": []},
                {"goal": "check the garden valve", "domain_name": "garden", "depends_on": []},
            ]
        }
    )
    h.model.push(respond("rack done"))
    h.model.push(respond("valve done"))

    result = h.loop.handle_turn("check the rack switch and check the garden valve")

    by_goal = {item.goal: item for item in result.tasks}
    assert by_goal["check the rack switch"].domain_id == homelab.id
    assert by_goal["check the garden valve"].domain_id == garden.id
    assert h.memory.get(lab_memory.id).scope == f"domain:{homelab.id}"
    assert h.memory.get(garden_memory.id).scope == f"domain:{garden.id}"

    # Each task's context keeps its own project primary while still allowing
    # cross-project knowledge to be relevant.
    lab_context = h.context.build(by_goal["check the rack switch"], h.sessions.current())
    garden_context = h.context.build(by_goal["check the garden valve"], h.sessions.current())
    assert f"Domain: homelab ({homelab.id})" in lab_context.messages[0].content
    assert f"Domain: garden ({garden.id})" in garden_context.messages[0].content
    assert lab_context.stats["project"]["primary_domain_id"] == homelab.id
    assert garden_context.stats["project"]["primary_domain_id"] == garden.id


def test_a_brief_unrelated_topic_does_not_create_or_suggest_a_domain(make_harness) -> None:
    h = make_harness(
        [respond("Sunny."), respond("Still sunny."), respond("Yes, sunny.")],
        settings={"tasks": {"planning_mode": "heuristic"}},
    )
    for _ in range(3):
        h.loop.handle_turn("weather")

    assert [item.id for item in h.domains.list_domains()] == ["self"]
    assert h.domains.list_suggestions() == []


def test_conflicting_and_superseded_memories_never_override_the_newer_fact(make_harness) -> None:
    h = make_harness([])
    old = remember(h, "The user's NAS is called vault", type="fact")
    new = remember(h, "The user's NAS is called archive", type="fact")

    assert h.memory.get(old.id).status == MemoryStatus.SUPERSEDED
    ranked = h.memory.retrieve("NAS called vault archive", ["global"], 5)
    ids = [item.record.id for item in ranked]
    assert new.id in ids
    assert old.id not in ids

    task = h.tasks.create(TaskSpec(goal="what is my NAS called?"))
    prompt = h.context.build(task, h.sessions.current()).messages[0].content
    assert new.content in prompt
    # The superseded fact must not be served as a memory block; verbatim
    # history of the earlier turn is a different, explicitly bounded section.
    assert f'<memory id="{old.id}"' not in prompt


def test_user_correction_replaces_an_outdated_preference(make_harness) -> None:
    h = make_harness([])
    old = remember(h, "The user prefers concise answers", type="preference")
    corrected = h.memory.submit(
        MemoryCandidate(
            content="Actually the user prefers detailed answers",
            origin="explicit",
            type_hint=old.type,
            kind_hint=old.kind,
            scope_hint=old.scope,
            default_scope=old.scope,
            supersedes=old.id,
            provenance={
                "source_kind": "user_statement",
                "source_refs": [h.sessions.record_interaction("correction").id],
                "evidence_status": "stated",
            },
        )
    )
    assert corrected.record is not None
    assert h.memory.get(old.id).status == MemoryStatus.SUPERSEDED

    task = h.tasks.create(TaskSpec(goal="how should you answer me?"))
    prompt = h.context.build(task, h.sessions.current()).messages[0].content
    assert corrected.record.content in prompt
    assert f'<memory id="{old.id}"' not in prompt


def test_budget_keeps_safety_and_pending_decisions(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="apply the pending change"))
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    h.decisions.create(
        Decision(
            id="dec_budget",
            kind="action",
            status=DecisionStatus.PENDING.value,
            created_at=now,
            updated_at=now,
            prompt="Apply the pending change?",
            summary="Apply the pending production change",
            task_id=task.id,
            risk="write",
        )
    )
    for index in range(30):
        remember(h, f"Low-value archive note number {index} about " + ("filler " * 20))
    h.settings.set("context.budgets.total", 1200, actor="test")
    h.settings.set("context.budgets.domain", 200, actor="test")

    package = h.context.build(task, h.sessions.current(), trace_id="trc_budget_safety")
    prompt = package.messages[0].content

    # Safety and decision interpretation survive; low-value memories are trimmed.
    assert "You are the reasoning model inside a personal assistant harness" in prompt
    assert "Never invent tools" in prompt
    assert "Apply the pending production change" in prompt
    assert package.total_tokens <= int(h.settings.get("context.budgets.total"))
    assert any(item.item == "domain:dropped" or item.item == "domain:overflow" for item in package.excluded)
    assert package.stats["budget"]["total"] <= int(h.settings.get("context.budgets.total"))


def test_context_includes_a_pending_decision_for_the_related_project(make_harness) -> None:
    h = make_harness([])
    domain = h.domains.create_domain(draft("homelab"), confirmed=True)
    task = h.tasks.create(TaskSpec(goal="continue the storage upgrade", domain_id=domain.id))
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    h.decisions.create(
        Decision(
            id="dec_project",
            kind="action",
            status=DecisionStatus.PENDING.value,
            created_at=now,
            updated_at=now,
            prompt="Restart the storage node?",
            summary="Restart the storage node",
            task_id=None,
            domain_id=domain.id,
            scope={"domain_id": domain.id},
            risk="destructive",
        )
    )

    prompt = h.context.build(task, h.sessions.current()).messages[0].content
    assert "Restart the storage node" in prompt


def test_relevant_history_and_memory_survive_a_new_harness_instance(make_harness) -> None:
    h = make_harness([])
    domain = h.domains.create_domain(draft("homelab"), confirmed=True)
    record = remember(h, "The workshop NAS is named timber", scope=f"domain:{domain.id}")
    h.sessions.record_interaction("Earlier we named the workshop NAS timber")

    reopened = build_harness(h.data_dir, model=ScriptedAdapter([]))
    task = reopened.tasks.create(TaskSpec(goal="what is the workshop NAS called?"))

    package = reopened.context.build(task, reopened.sessions.current())
    assert record.id in package.messages[0].content
    assert any("workshop NAS" in item.user_text for item in reopened.sessions.list_interactions(limit=None))


def test_retrieval_records_an_explainable_reason(make_harness) -> None:
    h = make_harness([])
    record = remember(h, "The workshop NAS is named timber")
    ranked = h.memory.retrieve("workshop timber", ["global"], 3)
    assert ranked[0].record.id == record.id
    assert {"lexical", "semantic", "scope", "recency", "provenance"} <= set(ranked[0].score_parts)

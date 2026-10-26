from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harness.contracts.memory import MemoryCandidate
from harness.contracts.tasks import TaskSpec


def remember(harness, content: str, *, scope: str = "global", type: str = "fact"):
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


def test_context_contains_ordered_sections_and_capability_disclosure(make_harness) -> None:
    h = make_harness([])
    task = h.tasks.create(TaskSpec(goal="hello"))
    package = h.context.build(task, h.sessions.current(), trace_id="trc_test")
    names = [item.name for item in package.sections]
    assert names == [
        "base_instructions",
        "persona",
        "preferences",
        "structure",
        "domain",
        "task",
        "history",
        "capabilities",
    ]
    prompt = package.messages[0].content
    assert "personal assistant harness" in prompt
    assert "mock" in prompt
    assert package.total_tokens == sum(item.tokens for item in package.sections)
    # llama.cpp and other chat templates require a user turn.
    assert package.messages[0].role == "system"
    assert package.messages[-1].role == "user"
    assert package.messages[-1].content == task.goal
    # Operation signatures must be disclosed, not just operation names.
    assert "echo (read)" in prompt
    assert "args: text:string; required: text" in prompt
    assert "add (read)" in prompt
    assert "args: a:number, b:number; required: a, b" in prompt
    assert "suggest_domain (read)" in prompt
    assert "required: name" in prompt
    assert "prose-only suggestion is not recorded" in prompt
    # Enum values come from the real enums, and optional hints are marked.
    assert "type:string (one of fact|preference|decision|procedure|lesson) [optional]" in prompt
    assert "scope:string (one of global|self|domain:<id>) [optional]" in prompt
    assert "content:string" in prompt
    assert "Current time:" in prompt


def test_structure_section_lists_active_domain_and_suggestions(make_harness) -> None:
    from harness.contracts.domains import StructureDraft

    h = make_harness([])
    domain = h.domains.create_domain(
        StructureDraft(name="Home Lab", reason="test", evidence="test"), confirmed=True
    )
    suggestion = h.domains.record_suggestion(
        StructureDraft(name="Future Lab", reason="test", evidence="test")
    )
    h.sessions.set_active_domain(domain.id)
    task = h.tasks.create(TaskSpec(goal="hello"))

    package = h.context.build(task, h.sessions.current(), trace_id="trc_structure")
    prompt = package.messages[0].content
    names = [item.name for item in package.sections]
    assert names.index("preferences") < names.index("structure") < names.index("domain")
    assert "## structure" in prompt
    assert f"Active domain: Home Lab ({domain.id})" in prompt
    assert f"- {domain.id} | Home Lab" in prompt
    assert f"- {suggestion.id} | Future Lab" in prompt
    # Non-archived domains include the reserved self domain.
    assert "- self | self" in prompt
    assert "Projects:" not in prompt


def test_structure_budget_drops_whole_entries_not_partial_lines(make_harness) -> None:
    from harness.contracts.domains import StructureDraft

    h = make_harness([], settings={"context": {"budgets": {"structure": 20}}})
    domain = h.domains.create_domain(
        StructureDraft(name="Home Lab", reason="test", evidence="test"), confirmed=True
    )
    suggestion = h.domains.record_suggestion(
        StructureDraft(name="Future Lab", reason="test", evidence="test")
    )
    h.sessions.set_active_domain(domain.id)
    task = h.tasks.create(TaskSpec(goal="hello"))

    package = h.context.build(task, h.sessions.current(), trace_id="trc_structure_budget")
    section = next(item for item in package.sections if item.name == "structure")
    assert section.tokens <= 20
    assert f"Active domain: Home Lab ({domain.id})" in section.text
    # Whole groups are dropped, never character-truncated mid-line.
    assert "Domains:" not in section.text
    assert suggestion.id not in section.text
    assert any(item.item == "structure:overflow" for item in package.excluded)


def test_memory_tag_includes_kind(make_harness) -> None:
    h = make_harness([])
    semantic = remember(h, "The NAS is at 192.168.1.10")
    procedural = remember(h, "Always restart the service after an update", type="lesson")
    assert semantic is not None and procedural is not None
    task = h.tasks.create(TaskSpec(goal="NAS restart service"))
    prompt = h.context.build(task, h.sessions.current(), trace_id="trc_kind").messages[0].content
    assert f'<memory id="{semantic.id}" kind="semantic"' in prompt
    assert f'<memory id="{procedural.id}" kind="procedural"' in prompt


def test_old_memory_carries_age_marker(make_harness) -> None:
    h = make_harness([], settings={"context": {"history": {"strategy": "full"}}})
    interaction = h.sessions.record_interaction("source for the garage code")
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    record = h.memory.submit(
        MemoryCandidate(
            content="The garage door code is 4411",
            origin="explicit",
            type_hint="fact",
            scope_hint="global",
            default_scope="global",
            provenance={
                "source_kind": "user_statement",
                "source_refs": [interaction.id],
                "evidence_status": "stated",
            },
            updated_at=old,
        )
    ).record
    task = h.tasks.create(TaskSpec(goal="what is the garage door code"))
    prompt = h.context.build(task, h.sessions.current(), trace_id="trc_memory_age").messages[0].content
    assert record.content in prompt
    assert 'age="10 days"' in prompt


def test_resumed_task_uses_latest_user_reply(make_harness) -> None:
    h = make_harness([])
    h.sessions.record_interaction("banana")
    task = h.tasks.create(TaskSpec(goal="Echo the value in <text>."))
    h.tasks.append_constraint(task.id, "User reply: banana")
    package = h.context.build(h.tasks.get(task.id), h.sessions.current(), trace_id="trc_resume")
    assert package.messages[-1].role == "user"
    assert package.messages[-1].content == "banana"
    assert "Resumed: the latest user message answers your pending question." in package.messages[0].content


def test_global_preference_appears_once_and_is_used(make_harness) -> None:
    h = make_harness([])
    record = remember(h, "The user prefers concise answers", type="preference")
    task = h.tasks.create(TaskSpec(goal="concise answer about concise answers"))
    package = h.context.build(task, h.sessions.current(), trace_id="trc_pref")
    prompt = package.messages[0].content
    assert prompt.count(record.id) == 1
    assert any(item.item == record.id and item.section == "preferences" for item in package.included)
    usage = h.memory.usage_log()
    assert any(item.memory_id == record.id and item.trace_id == "trc_pref" for item in usage)


def test_self_scope_loads_only_on_demand(make_harness) -> None:
    h = make_harness([])
    record = remember(
        h,
        "The assistant prefers a concise style for itself",
        scope="self",
        type="preference",
    )
    ordinary = h.tasks.create(TaskSpec(goal="book a table"))
    package = h.context.build(ordinary, h.sessions.current(), trace_id="trc_ordinary")
    assert record.id not in package.messages[0].content

    about_assistant = h.tasks.create(TaskSpec(goal="What do you remember about your memory?"))
    package = h.context.build(about_assistant, h.sessions.current(), trace_id="trc_self")
    assert record.id in package.messages[0].content
    assert any(item.item == "self" and item.reason == "request is about the assistant" for item in package.included)

    explicit_self = h.tasks.create(TaskSpec(goal="inspect", domain_id="self"))
    package = h.context.build(explicit_self, h.sessions.current(), trace_id="trc_explicit_self")
    assert record.id in package.messages[0].content


def test_domain_scope_loads_domain_memory(make_harness) -> None:
    from harness.contracts.domains import StructureDraft

    h = make_harness([])
    domain = h.domains.create_domain(
        StructureDraft(name="homelab", reason="test", evidence="test"), confirmed=True
    )
    record = remember(h, "The rack is in the basement", scope=f"domain:{domain.id}")
    task = h.tasks.create(TaskSpec(goal="plan the upgrade", domain_id=domain.id))

    package = h.context.build(task, h.sessions.current(), trace_id="trc_domain")
    prompt = package.messages[0].content
    assert f"Domain: {domain.name}" in prompt
    assert record.id in prompt
    included = {item.item for item in package.included}
    assert f"domain:{domain.id}" in included
    assert record.id in included


def test_section_over_budget_is_shortened_and_recorded(make_harness) -> None:
    h = make_harness([], settings={"context": {"budgets": {"history": 0, "capabilities": 0}}})
    h.sessions.record_interaction("a long history line that must not fit")
    h.sessions.append_assistant(h.sessions.current().id, "assistant reply")
    task = h.tasks.create(TaskSpec(goal="hello"))
    package = h.context.build(task, h.sessions.current(), trace_id="trc_budget")
    sections = {item.name: item for item in package.sections}
    assert sections["history"].tokens == 0
    assert sections["capabilities"].tokens == 0
    assert any(item.item == "history:overflow" for item in package.excluded)
    assert any(item.item == "capabilities:overflow" for item in package.excluded)


def test_base_instruction_overflow_is_not_silently_truncated(make_harness) -> None:
    h = make_harness([], settings={"context": {"budgets": {"base_instructions": 100}}})
    task = h.tasks.create(TaskSpec(goal="hello"))
    with pytest.raises(ValueError, match="base_instructions"):
        h.context.build(task, h.sessions.current(), trace_id="trc_overflow")


def test_trimmed_prompt_variant_is_selected_by_setting(make_harness) -> None:
    h = make_harness([], settings={"context": {"prompt_variant": "trimmed"}})
    task = h.tasks.create(TaskSpec(goal="hello"))
    prompt = h.context.build(task, h.sessions.current(), trace_id="trc_trimmed").messages[0].content
    assert "ask only for information from the user's world" in prompt
    assert "Decision order:" not in prompt
    assert "Never ask the user to choose internal details" in prompt


def test_enum_disclosure_can_be_disabled_for_the_experiment(make_harness) -> None:
    h = make_harness([], settings={"capabilities": {"disclose_enum_values": False}})
    task = h.tasks.create(TaskSpec(goal="hello"))
    prompt = h.context.build(task, h.sessions.current(), trace_id="trc_no_enum").messages[0].content
    assert "one of fact|preference" not in prompt
    assert "type:string" in prompt


def test_default_capability_setting_controls_disclosure(make_harness) -> None:
    h = make_harness([], settings={"capabilities": {"default_enabled": "mock"}})
    task = h.tasks.create(TaskSpec(goal="hello"))
    prompt = h.context.build(task, h.sessions.current(), trace_id="trc_caps").messages[0].content
    assert "mock:" in prompt
    assert "harness_memory:" not in prompt

from __future__ import annotations

from harness.constants import ExecutionStatus, PermissionLevel
from harness.contracts.capabilities import ExecuteRequest
from harness.contracts.domains import StructureDraft


def req(capability: str, operation: str, arguments: dict | None = None, **overrides) -> ExecuteRequest:
    values = {
        "task_id": "tsk_test",
        "domain_id": None,
        "capability": capability,
        "operation": operation,
        "arguments": arguments or {},
    }
    values.update(overrides)
    return ExecuteRequest(**values)


def test_unknown_capability_and_operation_are_unavailable(make_harness) -> None:
    h = make_harness([])
    missing = h.capabilities.execute(req("shell", "run", {"command": "ls"}))
    assert missing.status == ExecutionStatus.UNAVAILABLE
    assert missing.error_category == "not_registered"

    unknown_op = h.capabilities.execute(req("mock", "does_not_exist"))
    assert unknown_op.status == ExecutionStatus.UNAVAILABLE
    assert unknown_op.error_category == "not_found"


def test_operations_validate_required_and_extra_arguments(make_harness) -> None:
    h = make_harness([])
    missing = h.capabilities.execute(req("mock", "echo"))
    assert missing.status == ExecutionStatus.FAILED
    assert "missing required argument: text" in missing.message

    extra = h.capabilities.execute(req("mock", "echo", {"text": "x", "other": 1}))
    assert extra.status == ExecutionStatus.FAILED
    assert "unexpected argument" in extra.message

    wrong_type = h.capabilities.execute(req("mock", "echo", {"text": 12}))
    assert wrong_type.status == ExecutionStatus.FAILED
    assert "must be a string" in wrong_type.message

    boolean_number = h.capabilities.execute(req("mock", "add", {"a": True, "b": 2}))
    assert boolean_number.status == ExecutionStatus.FAILED
    assert "must be a number" in boolean_number.message


def test_successful_execution_records_attempt_and_event(make_harness) -> None:
    h = make_harness([])
    result = h.capabilities.execute(req("mock", "echo", {"text": "hello"}))
    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.data["echo"] == "hello"
    assert result.evidence["started_at"] and result.evidence["ended_at"]
    assert h.capabilities.get_attempt(result.operation_id).operation == "echo"
    assert [item.operation_id for item in h.capabilities.last_attempts("tsk_test")] == [result.operation_id]
    events = h.events.query(operation="capability.execute")
    assert events[-1].target == result.operation_id


def test_write_and_destructive_permissions_default_to_deny(make_harness) -> None:
    h = make_harness([])
    write = h.capabilities.execute(req("mock", "write_note", {"text": "hello"}))
    assert write.status == ExecutionStatus.DENIED
    assert write.error_category == "permission_denied"
    assert "capabilities.policy.write" in write.message

    destructive = h.capabilities.execute(req("mock", "delete_note", {"name": "x"}))
    assert destructive.status == ExecutionStatus.DENIED
    assert "capabilities.policy.destructive" in destructive.message


def test_permission_override_and_writable_policy_allow_write(make_harness) -> None:
    h = make_harness([], settings={"capabilities": {"policy": {"write": "allow"}}})
    result = h.capabilities.execute(req("mock", "write_note", {"text": "hello"}))
    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.data["idempotency_key"] == result.operation_id

    override = h.capabilities.execute(req("mock", "delete_note", {"name": "x"}, permission_override=True))
    assert override.status == ExecutionStatus.SUCCEEDED


def test_domain_enablement_denies_unlisted_capabilities(make_harness) -> None:
    h = make_harness([])
    domain = h.domains.create_domain(StructureDraft("domain", "homelab", "test", "test"), confirmed=True)
    h.domains.set_enablement(domain.id, [])
    denied = h.capabilities.execute(req("mock", "ping", domain_id=domain.id))
    assert denied.status == ExecutionStatus.DENIED
    assert denied.error_category == "not_enabled"

    h.domains.set_enablement(domain.id, ["mock"])
    allowed = h.capabilities.execute(req("mock", "ping", domain_id=domain.id))
    assert allowed.status == ExecutionStatus.SUCCEEDED


def test_capability_scripted_unknown_is_reported_as_unknown(make_harness) -> None:
    h = make_harness([], settings={"capabilities": {"policy": {"write": "allow"}}})
    h.capabilities.script_next(
        "mock",
        {"status": ExecutionStatus.UNKNOWN, "data": {}, "message": "timeout after send"},
    )
    result = h.capabilities.execute(req("mock", "write_note", {"text": "hello"}))
    assert result.status == ExecutionStatus.UNKNOWN
    assert "timeout" in result.message


def test_structure_capability_only_records_a_suggestion(make_harness) -> None:
    h = make_harness([])
    before = len(h.domains.list_domains())
    result = h.capabilities.execute(req("harness_structure", "suggest_domain", {"name": "homelab"}))
    assert result.status == ExecutionStatus.SUCCEEDED
    assert len(h.domains.list_domains()) == before
    suggestions = h.domains.list_suggestions()
    assert len(suggestions) == 1 and suggestions[0].status == "pending"


def test_memory_capability_uses_user_statement_sources(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("I prefer concise answers")
    result = h.capabilities.execute(
        req(
            "harness_memory",
            "remember",
            {"type": "preference", "content": "The user prefers concise answers", "scope": "global"},
            interaction_id=interaction.id,
        )
    )
    assert result.status == ExecutionStatus.SUCCEEDED
    record = h.memory.get(result.data["memory_id"])
    assert record.provenance["source_kind"] == "user_statement"

    retracted = h.capabilities.execute(req("harness_memory", "retract", {"memory_id": record.id}))
    assert retracted.status == ExecutionStatus.SUCCEEDED
    assert h.memory.get(record.id).status == "retracted"


def test_invalid_optional_memory_type_and_scope_are_coerced(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("Remember my name")
    result = h.capabilities.execute(
        req(
            "harness_memory",
            "remember",
            {"type": "identity", "content": "The user's name is Alex", "scope": "personal"},
            interaction_id=interaction.id,
        )
    )
    assert result.status == ExecutionStatus.SUCCEEDED
    record = h.memory.get(result.data["memory_id"])
    assert record.type == "fact"
    assert record.scope == "global"
    notes = " ".join(result.data["argument_notes"])
    assert "identity" in notes and "fact" in notes
    assert "personal" in notes


def test_literal_scope_pattern_is_treated_as_a_hint_not_an_id(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("Remember my name")
    result = h.capabilities.execute(
        req(
            "harness_memory",
            "remember",
            {"content": "The user's name is Alex", "scope": "domain:<id>"},
            interaction_id=interaction.id,
        )
    )
    assert result.status == ExecutionStatus.SUCCEEDED
    record = h.memory.get(result.data["memory_id"])
    assert record.scope == "global"
    assert "domain:<id>" in " ".join(result.data["argument_notes"])


def test_self_scope_hint_is_overridden_for_a_user_fact(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("Remember my name")
    result = h.capabilities.execute(
        req(
            "harness_memory",
            "remember",
            {"content": "The user's name is Alex", "scope": "self"},
            interaction_id=interaction.id,
        )
    )
    assert result.status == ExecutionStatus.SUCCEEDED
    record = h.memory.get(result.data["memory_id"])
    assert record.scope == "global"
    assert "only for memories about the assistant" in " ".join(result.data["argument_notes"])


def test_self_scope_is_kept_for_a_task_about_the_assistant(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("Remember that you are my assistant")
    result = h.capabilities.execute(
        req(
            "harness_memory",
            "remember",
            {"content": "The assistant is called Jarvis", "scope": "self"},
            interaction_id=interaction.id,
            self_request=True,
        )
    )
    assert result.status == ExecutionStatus.SUCCEEDED
    record = h.memory.get(result.data["memory_id"])
    assert record.scope == "self"
    assert "argument_notes" not in result.data


def test_bad_memory_hints_are_coerced_even_when_general_coercion_is_off(make_harness) -> None:
    h = make_harness([], settings={"capabilities": {"coerce_optional_arguments": False}})
    interaction = h.sessions.record_interaction("Remember my name")
    result = h.capabilities.execute(
        req(
            "harness_memory",
            "remember",
            {"type": "identity", "content": "The user's name is Alex", "scope": "personal"},
            interaction_id=interaction.id,
        )
    )
    assert result.status == ExecutionStatus.SUCCEEDED
    record = h.memory.get(result.data["memory_id"])
    assert record.type == "fact"
    assert record.scope == "global"
    notes = " ".join(result.data["argument_notes"])
    assert "identity" in notes and "fact" in notes
    assert "personal" in notes


def test_validation_reports_every_bad_field_at_once(make_harness) -> None:
    h = make_harness([])
    result = h.capabilities.execute(
        req("mock", "add", {"a": "two", "b": 3, "extra": True})
    )
    assert result.status == ExecutionStatus.FAILED
    assert 'invalid a "two": must be a number' in result.message
    assert 'unexpected argument "extra"' in result.message
    assert len(result.data["argument_errors"]) == 2


def test_unresolved_placeholder_is_rejected_before_dispatch(make_harness) -> None:
    h = make_harness([])
    result = h.capabilities.execute(
        req("mock", "echo", {"text": "Echo the value in <text>."})
    )
    assert result.status == ExecutionStatus.FAILED
    assert "unresolved placeholder <text>" in result.message
    assert "ask the user for the real value" in result.message


def test_placeholder_rejection_can_be_disabled_for_legacy_comparison(make_harness) -> None:
    h = make_harness([], settings={"capabilities": {"reject_placeholders": False}})
    result = h.capabilities.execute(
        req("mock", "echo", {"text": "Echo the value in <text>."})
    )
    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.data["echo"] == "Echo the value in <text>."


def test_memory_defaults_type_and_derives_scope_from_task(make_harness) -> None:
    from harness.contracts.domains import StructureDraft

    h = make_harness([])
    domain = h.domains.create_domain(StructureDraft("domain", "homelab", "test", "test"), confirmed=True)
    interaction = h.sessions.record_interaction("Remember the NAS address")
    result = h.capabilities.execute(
        req(
            "harness_memory",
            "remember",
            {"content": "The NAS is at 192.168.1.10"},
            interaction_id=interaction.id,
            domain_id=domain.id,
        )
    )
    assert result.status == ExecutionStatus.SUCCEEDED
    record = h.memory.get(result.data["memory_id"])
    assert record.type == "fact"
    assert record.scope == f"domain:{domain.id}"


def test_duplicate_remember_is_idempotent(make_harness) -> None:
    h = make_harness([])
    interaction = h.sessions.record_interaction("Remember my name is Alex")
    first = h.capabilities.execute(
        req("harness_memory", "remember", {"content": "The user's name is Alex"}, interaction_id=interaction.id)
    )
    second = h.capabilities.execute(
        req("harness_memory", "remember", {"content": "The user's name is Alex."}, interaction_id=interaction.id)
    )
    assert first.status == ExecutionStatus.SUCCEEDED
    assert second.status == ExecutionStatus.SUCCEEDED
    assert second.data["duplicate"] is True
    assert second.data["memory_id"] == first.data["memory_id"]
    assert len(h.memory.list(status="active")) == 1


def test_conflicting_remember_reports_the_replaced_memory(make_harness) -> None:
    h = make_harness([])
    first_source = h.sessions.record_interaction("Remember my name is Alex")
    first = h.capabilities.execute(
        req("harness_memory", "remember", {"content": "The user's name is Alex"}, interaction_id=first_source.id)
    )
    second_source = h.sessions.record_interaction("Actually my name is Bob")
    second = h.capabilities.execute(
        req("harness_memory", "remember", {"content": "The user's name is Bob"}, interaction_id=second_source.id)
    )
    assert second.status == ExecutionStatus.SUCCEEDED
    assert "replaced previous memory" in second.message
    assert second.data["replaced"][0]["id"] == first.data["memory_id"]
    assert h.memory.get(first.data["memory_id"]).status == "superseded"
    assert h.memory.get(second.data["memory_id"]).status == "active"


def test_manifest_operations_have_permission_levels(make_harness) -> None:
    h = make_harness([])
    manifest = h.capabilities.get("mock")
    permissions = {item.name: item.permission for item in manifest.operations}
    assert permissions["status"] == PermissionLevel.READ
    assert permissions["write_note"] == PermissionLevel.WRITE
    assert permissions["delete_note"] == PermissionLevel.DESTRUCTIVE
    assert manifest.available is True

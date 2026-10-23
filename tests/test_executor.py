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


def test_manifest_operations_have_permission_levels(make_harness) -> None:
    h = make_harness([])
    manifest = h.capabilities.get("mock")
    permissions = {item.name: item.permission for item in manifest.operations}
    assert permissions["status"] == PermissionLevel.READ
    assert permissions["write_note"] == PermissionLevel.WRITE
    assert permissions["delete_note"] == PermissionLevel.DESTRUCTIVE
    assert manifest.available is True

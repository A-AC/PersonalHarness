from __future__ import annotations

from typing import Any

from harness.constants import ExecutionStatus, PermissionLevel
from harness.contracts.capabilities import Manifest, OperationSpec


MOCK_MANIFEST = Manifest(
    name="mock",
    purpose="Scriptable test capability with no external effects.",
    source="mock",
    operations=[
        OperationSpec(name="ping", description="Returns pong.", permission=PermissionLevel.READ, idempotent=True),
        OperationSpec(
            name="echo",
            description="Returns the provided text.",
            permission=PermissionLevel.READ,
            inputs={"text": "string"},
            required=["text"],
            idempotent=True,
        ),
        OperationSpec(
            name="status",
            description="Returns a mock machine status for a named target.",
            permission=PermissionLevel.READ,
            inputs={"target": "string"},
            required=["target"],
            idempotent=True,
        ),
        OperationSpec(
            name="add",
            description="Adds two numbers.",
            permission=PermissionLevel.READ,
            inputs={"a": "number", "b": "number"},
            required=["a", "b"],
            idempotent=True,
        ),
        OperationSpec(
            name="fail",
            description="Fails with a structured error.",
            permission=PermissionLevel.READ,
            inputs={"category": "string"},
            required=[],
            retryable_error_categories=["transient"],
        ),
        OperationSpec(
            name="write_note",
            description="Pretend write that can time out after sending.",
            permission=PermissionLevel.WRITE,
            inputs={"text": "string"},
            required=["text"],
            idempotent=False,
            accepts_idempotency_key=True,
        ),
        OperationSpec(
            name="delete_note",
            description="Pretend destructive delete.",
            permission=PermissionLevel.DESTRUCTIVE,
            inputs={"name": "string"},
            required=["name"],
            idempotent=False,
        ),
    ],
)


def run_mock(
    operation: str,
    arguments: dict[str, Any],
    scripted: dict[str, Any] | None,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if scripted is not None:
        return scripted
    if operation == "ping":
        return {"status": ExecutionStatus.SUCCEEDED, "data": {"pong": True}, "message": "pong"}
    if operation == "echo":
        return {"status": ExecutionStatus.SUCCEEDED, "data": {"echo": str(arguments.get("text", ""))[:500]}, "message": "echoed"}
    if operation == "status":
        target = str(arguments.get("target", ""))
        return {
            "status": ExecutionStatus.SUCCEEDED,
            "data": {"target": target, "online": True, "kind": "mock"},
            "message": f"{target} is online",
        }
    if operation == "fail":
        category = str(arguments.get("category") or "failed")
        return {"status": ExecutionStatus.FAILED, "data": {}, "error_category": category, "message": f"mock failure:{category}"}
    if operation == "add":
        return {
            "status": ExecutionStatus.SUCCEEDED,
            "data": {"sum": arguments["a"] + arguments["b"]},
            "message": "added",
        }
    if operation == "write_note":
        return {
            "status": ExecutionStatus.SUCCEEDED,
            "data": {"written": arguments.get("text"), "idempotency_key": idempotency_key},
            "message": "written",
        }
    if operation == "delete_note":
        return {"status": ExecutionStatus.SUCCEEDED, "data": {"deleted": arguments.get("name")}, "message": "deleted"}
    return {"status": ExecutionStatus.FAILED, "data": {}, "error_category": "unknown_operation", "message": operation}

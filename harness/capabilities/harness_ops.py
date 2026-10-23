from __future__ import annotations

from harness.constants import PermissionLevel
from harness.contracts.capabilities import Manifest, OperationSpec


MEMORY_MANIFEST = Manifest(
    name="harness_memory",
    purpose="Create, correct, or retract long-term memories.",
    source="harness",
    operations=[
        OperationSpec(
            name="remember",
            description=(
                "Store an explicit memory from the user's statement. type defaults to fact; "
                "allowed types: fact, preference, decision, procedure, lesson. scope defaults "
                "to global; allowed scopes: global, self, domain:<id>, project:<id>."
            ),
            permission=PermissionLevel.WRITE,
            inputs={"type": "string", "content": "string", "scope": "string"},
            required=["content"],
        ),
        OperationSpec(
            name="correct",
            description="Supersede an existing memory with a correction.",
            permission=PermissionLevel.WRITE,
            inputs={"memory_id": "string", "content": "string"},
            required=["memory_id", "content"],
        ),
        OperationSpec(
            name="retract",
            description="Retract a memory.",
            permission=PermissionLevel.WRITE,
            inputs={"memory_id": "string"},
            required=["memory_id"],
        ),
    ],
)


STRUCTURE_MANIFEST = Manifest(
    name="harness_structure",
    purpose="Propose persistent structure for explicit user confirmation.",
    source="harness",
    operations=[
        OperationSpec(
            name="suggest_domain",
            description=(
                "Record a pending domain suggestion for user confirmation. Use this for "
                "requests to suggest or propose a domain, including when the user says "
                "not to create it. Never creates the domain."
            ),
            permission=PermissionLevel.READ,
            inputs={"name": "string", "reason": "string"},
            required=["name"],
        ),
    ],
)

from __future__ import annotations

from harness.constants import MEMORY_SCOPE_PREFIXES, MemoryScopeKind, MemoryType, PermissionLevel
from harness.contracts.capabilities import Manifest, OperationSpec


# Generated from the real enums. Do not hand-write allowed values in an
# operation description: the context builder renders these lists inline.
MEMORY_TYPES = tuple(item.value for item in MemoryType)
MEMORY_SCOPES = tuple(item.value for item in MemoryScopeKind) + tuple(
    f"{prefix}:<id>" for prefix in MEMORY_SCOPE_PREFIXES
)


MEMORY_MANIFEST = Manifest(
    name="harness_memory",
    purpose="Create, correct, or retract long-term memories.",
    source="harness",
    operations=[
        OperationSpec(
            name="remember",
            description=(
                "Store a memory from an explicit user statement. Content must be a clear, "
                "standalone statement. Type and scope are optional hints; the harness derives "
                "them, so never ask the user to choose them."
            ),
            permission=PermissionLevel.WRITE,
            inputs={"type": "string", "content": "string", "scope": "string"},
            required=["content"],
            enums={"type": MEMORY_TYPES, "scope": MEMORY_SCOPES},
            hint_inputs=["type", "scope"],
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
    purpose=(
        "Work inside existing domains and propose persistent structure for explicit "
        "user confirmation."
    ),
    source="harness",
    operations=[
        OperationSpec(
            name="enter_domain",
            description=(
                "Make an existing domain the active domain. Use this for 'work on', "
                "'switch to', or 'open <domain>'. Resolves an existing non-archived "
                "domain by exact or normalized name and never creates one. A missing or "
                "ambiguous name fails with candidate names."
            ),
            permission=PermissionLevel.READ,
            inputs={"name": "string"},
            required=["name"],
        ),
        OperationSpec(
            name="leave_domain",
            description=(
                "Clear the active domain and return to no active domain. The next task uses "
                "the global/default scope until another domain is entered."
            ),
            permission=PermissionLevel.READ,
            inputs={},
        ),
        OperationSpec(
            name="suggest_domain",
            description=(
                "Record a pending domain suggestion for user confirmation. Use this for "
                "requests to suggest or propose a domain, including when the user says "
                "not to create it. Never creates the domain. An existing domain, a "
                "pending duplicate, or an active decline cooldown is reported instead "
                "of recording another suggestion."
            ),
            permission=PermissionLevel.READ,
            inputs={"name": "string", "reason": "string"},
            required=["name"],
        ),
    ],
)

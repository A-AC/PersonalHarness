"""Structural constants only: schema versions and enumerations."""

from __future__ import annotations

from enum import StrEnum

SCHEMA_VERSION = 1

ID_PREFIXES = (
    "evt",
    "trc",
    "int",
    "ses",
    "tsk",
    "dom",
    "mem",
    "run",
    "op",
    "sug",
    "per",
    "chg",
    "dec",
)


class TaskState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BlockReason(StrEnum):
    AWAITING_USER = "awaiting_user"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    DEPENDENCY = "dependency"
    LIMIT = "limit"
    MODEL_UNAVAILABLE = "model_unavailable"
    INTERRUPTED = "interrupted"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"


class DecisionStatus(StrEnum):
    """Lifecycle of a durable confirmation/decision.

    ``pending`` and ``deferred`` are open decisions. The remaining values are
    terminal for one proposal; a new request must create a new decision.
    """

    PENDING = "pending"
    DEFERRED = "deferred"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class DecisionIntent(StrEnum):
    """How a user message relates to an open decision."""

    APPROVE = "approve"
    REJECT = "reject"
    MODIFY = "modify"
    DEFER = "defer"
    CANCEL = "cancel"
    CLARIFY = "clarify"
    UNRELATED = "unrelated"
    NONE = "none"


class MemoryKind(StrEnum):
    """Orthogonal memory axis; ``MemoryType`` stays the semantic subtype."""

    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"
    PROSPECTIVE = "prospective"


class MemoryType(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    PROCEDURE = "procedure"
    LESSON = "lesson"


# Deterministic kind derivation for legacy rows and new writes. ``procedure``
# and ``lesson`` remain valid types so old records load unchanged; they map to
# the procedural kind instead of being renamed on disk.
MEMORY_KIND_BY_TYPE: dict[str, MemoryKind] = {
    MemoryType.FACT: MemoryKind.SEMANTIC,
    MemoryType.PREFERENCE: MemoryKind.SEMANTIC,
    MemoryType.DECISION: MemoryKind.SEMANTIC,
    MemoryType.PROCEDURE: MemoryKind.PROCEDURAL,
    MemoryType.LESSON: MemoryKind.PROCEDURAL,
}


def memory_kind_for_type(type_name: str) -> str:
    """Return the kind implied by a type, defaulting to semantic."""

    return str(MEMORY_KIND_BY_TYPE.get(str(type_name).strip(), MemoryKind.SEMANTIC))


class MemoryScopeKind(StrEnum):
    GLOBAL = "global"
    SELF = "self"


# Scoped memory ids are dynamic, so they are disclosed as patterns rather than
# a closed enum. The capability description and the validator both use these.
MEMORY_SCOPE_PREFIXES = ("domain",)


class MemoryOrigin(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    CONSOLIDATED = "consolidated"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class SourceKind(StrEnum):
    USER_STATEMENT = "user_statement"
    EXECUTION_OUTCOME = "execution_outcome"
    DOCUMENT = "document"
    IMPORTED = "imported"


class EvidenceStatus(StrEnum):
    STATED = "stated"
    OBSERVED = "observed"
    INFERRED = "inferred"
    CORROBORATED = "corroborated"


class ActionKind(StrEnum):
    RESPOND = "respond"
    ASK = "ask"
    ACT = "act"
    CONTINUE = "continue"
    END = "end"


class EndStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class PermissionLevel(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class Actor(StrEnum):
    USER = "user"
    MODEL = "model"
    HARNESS = "harness"
    CONSOLIDATION = "consolidation"


SELF_DOMAIN_ID = "self"
SELF_DOMAIN_NAME = "self"

# Source references that point at conversation/execution rows. A
# conversations-only reset removes those rows, so retained memory provenance
# drops the ids (and records what was dropped) instead of leaving dangling refs.
CONVERSATION_REF_PREFIXES = ("int_", "op_", "evt_", "trc_", "tsk_")

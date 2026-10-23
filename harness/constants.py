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
    "prj",
    "wks",
    "mem",
    "run",
    "op",
    "sug",
    "per",
    "chg",
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
    DEPENDENCY = "dependency"
    LIMIT = "limit"
    MODEL_UNAVAILABLE = "model_unavailable"
    INTERRUPTED = "interrupted"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"


class MemoryType(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    PROCEDURE = "procedure"
    LESSON = "lesson"


class MemoryOrigin(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    CONSOLIDATED = "consolidated"


class MemoryStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    REJECTED = "rejected"


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

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
    "lrn",
    "skl",
)


class TaskState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    # Deferred work is explicitly postponed: it stays out of the eligible set
    # until its defer window passes (and no open decision still holds it).
    DEFERRED = "deferred"
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
    # An attempt was left in flight (process died mid-dispatch); the outcome
    # must be reconciled or confirmed by the user before any retry.
    UNKNOWN_OUTCOME = "unknown_outcome"
    # The scheduler will not requeue the task again; retry budget exhausted.
    RETRY_LIMIT = "retry_limit"
    # The task is deferred by the user or by scheduler backoff.
    DEFERRED = "deferred"


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


class MemoryLifecycle(StrEnum):
    """A derived, explainable view over the stored lifecycle metadata.

    This is not a replacement for ``status``, ``type``, or ``kind``. It makes
    it explicit whether a record is a verified statement, a user preference, a
    decision, a lesson, a temporary event, an unresolved hypothesis, or a
    proposed future commitment. The stored fields remain the source of truth.
    """

    VERIFIED_FACT = "verified_fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    LESSON = "lesson"
    TEMPORARY_EVENT = "temporary_event"
    HYPOTHESIS = "hypothesis"
    COMMITMENT = "commitment"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class SourceKind(StrEnum):
    USER_STATEMENT = "user_statement"
    EXECUTION_OUTCOME = "execution_outcome"
    DOCUMENT = "document"
    IMPORTED = "imported"


class LearningSignalKind(StrEnum):
    """What kind of observation is being offered to the learning pipeline.

    The kind says where the evidence came from, not what should be learned;
    the pipeline maps a signal kind onto a learning category and an evidence
    threshold before anything is retained.
    """

    USER_CORRECTION = "user_correction"
    REPEATED_PREFERENCE = "repeated_preference"
    TASK_SUCCESS = "task_success"
    VERIFIED_FAILURE = "verified_failure"
    REPEATED_WORKFLOW = "repeated_workflow"
    REJECTED_BEHAVIOR = "rejected_behavior"
    RECURRING_COMMITMENT = "recurring_commitment"
    TRANSIENT_OBSERVATION = "transient_observation"
    STRATEGY_PROPOSAL = "strategy_proposal"


class LearningCategory(StrEnum):
    """The three types of learning. They have different risk profiles and
    different authorization levels, so they are never conflated:

    - ``memory``: a durable fact/preference/decision/lesson, provenance-scoped,
      reversible through supersession/retraction.
    - ``procedure``: a reusable workflow turned into a staged skill candidate;
      it never reaches the prompt until it is tested and approved.
    - ``strategy``: a proposed change to how the assistant plans or decides;
      always staged (persona proposal) and safety-validated first.
    """

    MEMORY = "memory"
    PROCEDURE = "procedure"
    STRATEGY = "strategy"


class LearningDecision(StrEnum):
    """What the pipeline decided to do with one signal.

    ``applied`` changes effective behavior now (automatic, low risk, with
    rollback). ``staged`` created a candidate that waits for an explicit
    approval. ``rejected`` failed validation. ``insufficient`` did not have
    strong enough evidence. ``duplicate`` matched existing knowledge.
    """

    APPLIED = "applied"
    STAGED = "staged"
    REJECTED = "rejected"
    INSUFFICIENT = "insufficient"
    DUPLICATE = "duplicate"


class SkillStatus(StrEnum):
    """Lifecycle of a learned skill. Candidates are never loaded into the
    prompt; only ``approved`` skills are discoverable by the executor."""

    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETIRED = "retired"



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

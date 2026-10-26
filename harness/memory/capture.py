from __future__ import annotations

from harness.constants import (
    SELF_DOMAIN_ID,
    EvidenceStatus,
    ExecutionStatus,
    MemoryKind,
    MemoryOrigin,
    MemoryScopeKind,
    MemoryStatus,
    SourceKind,
)
from harness.contracts.actions import ExecutionResult
from harness.contracts.events import Event, EventLog
from harness.contracts.memory import (
    ExecutionOutcomeRecorder,
    MemoryCandidate,
    MemoryManager,
    MemoryRecord,
    OutcomeCapture,
)
from harness.contracts.settings import SettingsService
from harness.storage.atomic import new_id, now_ts

# Only these outcomes are worth a lesson. A routine success is silent; a
# success is captured only when it resolves a prior non-succeeded attempt.
CAPTURED_STATUSES = {
    ExecutionStatus.FAILED,
    ExecutionStatus.DENIED,
    ExecutionStatus.UNAVAILABLE,
    ExecutionStatus.UNKNOWN,
}

# Harness bookkeeping operations are never procedural lessons about the world.
EXCLUDED_CAPABILITIES = {"harness_memory", "harness_structure"}

FINGERPRINT_KEYS = ("capability", "operation", "status", "error_category")


def derived_scope(*, domain_id: str | None, self_request: bool) -> str:
    """A non-self domain wins, then self, then global."""
    if domain_id == SELF_DOMAIN_ID or self_request:
        return MemoryScopeKind.SELF.value
    if domain_id:
        return f"domain:{domain_id}"
    return MemoryScopeKind.GLOBAL.value


class ExecutionOutcomeCapture(ExecutionOutcomeRecorder):
    """Deterministic, no-model procedural memory from execution attempts.

    One record per distinct failure fingerprint (capability, operation, status,
    error_category) in a scope. A later success of the same operation within
    the same task writes a resolution lesson and supersedes the failure it
    resolves. Nothing is ever auto-retracted.
    """

    def __init__(
        self,
        *,
        settings: SettingsService,
        events: EventLog,
        memory: MemoryManager,
    ):
        self._settings = settings
        self._events = events
        self._memory = memory

    def record(
        self,
        execution: ExecutionResult,
        *,
        domain_id: str | None,
        self_request: bool,
        prior_failure: ExecutionResult | None = None,
        prior_failure_memory_id: str | None = None,
    ) -> OutcomeCapture | None:
        if not self._enabled():
            return None
        if execution.capability in EXCLUDED_CAPABILITIES:
            return None
        resolving = prior_failure is not None
        if resolving:
            if execution.status != ExecutionStatus.SUCCEEDED:
                return None
        elif execution.status not in CAPTURED_STATUSES:
            return None

        scope = derived_scope(domain_id=domain_id, self_request=self_request)
        fingerprint = {
            "capability": str(execution.capability),
            "operation": str(execution.operation),
            "status": str(execution.status),
            "error_category": None
            if execution.error_category is None
            else str(execution.error_category),
        }
        if not resolving:
            existing = self._find_fingerprint(scope, fingerprint)
            if existing is not None:
                # A repeated identical failure must not become one record per
                # attempt; return the live lesson so a later success can still
                # supersede it.
                return OutcomeCapture(
                    memory_id=existing.id, created=False, fingerprint=fingerprint
                )

        candidate = MemoryCandidate(
            content=self._content(execution, prior_failure),
            origin=MemoryOrigin.INFERRED,
            kind_hint=MemoryKind.PROCEDURAL,
            default_scope=scope,
            provenance={
                "source_kind": SourceKind.EXECUTION_OUTCOME,
                "source_refs": [execution.operation_id],
                "evidence_status": EvidenceStatus.OBSERVED,
                "structured_fields": {
                    "status": fingerprint["status"],
                    "error_category": fingerprint["error_category"],
                    "message": execution.message,
                    "capability": fingerprint["capability"],
                    "operation": fingerprint["operation"],
                },
            },
            supersedes=prior_failure_memory_id if resolving else None,
        )
        result = self._memory.submit(candidate)
        if not result.accepted or result.record is None:
            return None
        if result.duplicate:
            return OutcomeCapture(
                memory_id=result.record.id, created=False, fingerprint=fingerprint
            )
        record = result.record
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=now_ts(),
                actor="harness",
                operation="memory.capture",
                target=record.id,
                result=fingerprint["status"],
                refs=[execution.task_id] if execution.task_id else [],
                data={
                    **fingerprint,
                    "scope": scope,
                    "operation_id": execution.operation_id,
                    "resolving": resolving,
                },
            )
        )
        return OutcomeCapture(memory_id=record.id, created=True, fingerprint=fingerprint)

    def _enabled(self) -> bool:
        if not bool(self._settings.get("memory.capture.execution_outcomes")):
            return False
        policy = str(self._settings.get("memory.source_policy.execution_outcome"))
        return policy != "disabled"

    def _find_fingerprint(self, scope: str, fingerprint: dict) -> MemoryRecord | None:
        for record in self._memory.list(
            scope=scope, status=MemoryStatus.ACTIVE.value
        ):
            if record.kind != MemoryKind.PROCEDURAL:
                continue
            fields = (record.provenance or {}).get("structured_fields") or {}
            if all(fields.get(key) == fingerprint.get(key) for key in FINGERPRINT_KEYS):
                return record
        return None

    @staticmethod
    def _content(
        execution: ExecutionResult, prior_failure: ExecutionResult | None
    ) -> str:
        target = f"{execution.capability}.{execution.operation}"
        message = " ".join((execution.message or "").split()).rstrip(".")
        if prior_failure is not None:
            category = prior_failure.error_category or prior_failure.status
            prior_message = " ".join((prior_failure.message or "").split()).rstrip(".")
            detail = f"{category}: {prior_message}" if prior_message else str(category)
            return (
                f"Execution of {target} later succeeded after an earlier "
                f"{prior_failure.status} ({detail})."
            )
        category = execution.error_category or "no error category"
        detail = f"{category}: {message}" if message else str(category)
        return f"Execution of {target} {execution.status} ({detail})."

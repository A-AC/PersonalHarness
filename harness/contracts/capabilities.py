from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from harness.contracts.actions import ExecutionResult


@dataclass
class OperationSpec:
    name: str
    description: str
    permission: str
    inputs: dict[str, str] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    # Allowed values for enum-like arguments, generated from the real enums in
    # constants. ``domain:<id>`` is a pattern, not a literal value, and is
    # matched by prefix.
    enums: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Defaults for optional arguments. The harness applies them before dispatch.
    defaults: dict[str, Any] = field(default_factory=dict)
    # Optional inputs that are untrusted hints. Invalid values are always
    # coerced or dropped, even when general optional-argument coercion is off.
    hint_inputs: list[str] = field(default_factory=list)
    idempotent: bool = True
    accepts_idempotency_key: bool = False
    retryable_error_categories: list[str] = field(default_factory=list)
    # An operation may declare that a one-time confirmation is always needed.
    # Harness policy can additionally require confirmation for permission
    # classes (see control.confirmation.permissions).
    requires_confirmation: bool = False


@dataclass
class Manifest:
    name: str
    purpose: str
    source: str
    operations: list[OperationSpec]
    skill_body: str | None = None
    available: bool = True


@dataclass
class ArgumentValidation:
    """Side-effect-free argument check used before re-approving a modification."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    normalized_arguments: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    permission: str | None = None


@dataclass
class ExecuteRequest:
    task_id: str
    domain_id: str | None
    capability: str
    operation: str
    arguments: dict[str, Any]
    permission_override: bool = False
    interaction_id: str | None = None
    idempotency_key: str | None = None
    # True when the task is about the assistant itself, so memory scope "self"
    # is the derived scope instead of an override.
    self_request: bool = False


class CapabilityExecutor(Protocol):
    def list_manifests(self) -> list[Manifest]: ...
    def get(self, name: str) -> Manifest: ...
    def execute(self, req: ExecuteRequest) -> ExecutionResult: ...
    def validate_arguments(
        self,
        capability: str,
        operation: str,
        arguments: dict[str, Any],
    ) -> ArgumentValidation: ...
    def script_next(self, capability: str, envelope: dict[str, Any]) -> None: ...
    def last_attempts(self, task_id: str) -> list[ExecutionResult]: ...
    def get_attempt(self, operation_id: str) -> ExecutionResult | None: ...
    # All attempts grouped by task id from one scan; the scheduler and status
    # read model use this instead of per-task globbing.
    def attempts_by_task(self) -> dict[str, list[ExecutionResult]]: ...

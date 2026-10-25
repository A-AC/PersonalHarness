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
    # constants. ``domain:<id>`` and ``project:<id>`` are patterns, not literal
    # values, and are matched by prefix.
    enums: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Defaults for optional arguments. The harness applies them before dispatch.
    defaults: dict[str, Any] = field(default_factory=dict)
    # Optional inputs that are untrusted hints. Invalid values are always
    # coerced or dropped, even when general optional-argument coercion is off.
    hint_inputs: list[str] = field(default_factory=list)
    idempotent: bool = True
    accepts_idempotency_key: bool = False
    retryable_error_categories: list[str] = field(default_factory=list)


@dataclass
class Manifest:
    name: str
    purpose: str
    source: str
    operations: list[OperationSpec]
    skill_body: str | None = None
    available: bool = True


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
    project_id: str | None = None
    # True when the task is about the assistant itself, so memory scope "self"
    # is the derived scope instead of an override.
    self_request: bool = False


class CapabilityExecutor(Protocol):
    def list_manifests(self) -> list[Manifest]: ...
    def get(self, name: str) -> Manifest: ...
    def execute(self, req: ExecuteRequest) -> ExecutionResult: ...
    def script_next(self, capability: str, envelope: dict[str, Any]) -> None: ...
    def last_attempts(self, task_id: str) -> list[ExecutionResult]: ...
    def get_attempt(self, operation_id: str) -> ExecutionResult | None: ...

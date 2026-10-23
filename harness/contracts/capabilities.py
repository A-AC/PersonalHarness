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


class CapabilityExecutor(Protocol):
    def list_manifests(self) -> list[Manifest]: ...
    def get(self, name: str) -> Manifest: ...
    def execute(self, req: ExecuteRequest) -> ExecutionResult: ...
    def script_next(self, capability: str, envelope: dict[str, Any]) -> None: ...
    def last_attempts(self, task_id: str) -> list[ExecutionResult]: ...
    def get_attempt(self, operation_id: str) -> ExecutionResult | None: ...

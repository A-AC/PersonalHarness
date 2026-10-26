from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union


@dataclass
class RespondAction:
    message: str
    reason: str | None = None
    kind: Literal["respond"] = "respond"


@dataclass
class AskAction:
    question: str
    missing: str
    reason: str | None = None
    kind: Literal["ask"] = "ask"


@dataclass
class ActAction:
    capability: str
    operation: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    # The model may ask the harness to collect a one-time confirmation before
    # dispatch. This is a proposal, not an authorization: the harness still
    # enforces every capability and policy guard.
    requires_confirmation: bool = False
    risk: str | None = None
    kind: Literal["act"] = "act"


@dataclass
class ContinueAction:
    purpose: str
    reason: str | None = None
    kind: Literal["continue"] = "continue"


@dataclass
class EndAction:
    status: Literal["completed", "cancelled", "abandoned"]
    summary: str
    reason: str | None = None
    kind: Literal["end"] = "end"


Proposal = Union[RespondAction, AskAction, ActAction, ContinueAction, EndAction]


@dataclass
class ExecutionResult:
    operation_id: str
    task_id: str
    capability: str
    operation: str
    status: Literal["succeeded", "failed", "denied", "unavailable", "unknown"]
    data: dict[str, Any]
    message: str
    error_category: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

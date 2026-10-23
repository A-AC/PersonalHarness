from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from harness.contracts.actions import ActAction, AskAction, ContinueAction, EndAction, Proposal, RespondAction


class RespondModel(BaseModel):
    kind: str
    message: str
    reason: str | None = None


class AskModel(BaseModel):
    kind: str
    question: str
    missing: str
    reason: str | None = None


class ActModel(BaseModel):
    kind: str
    capability: str
    operation: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class ContinueModel(BaseModel):
    kind: str
    purpose: str
    reason: str | None = None


class EndModel(BaseModel):
    kind: str
    status: str
    summary: str
    reason: str | None = None


class TaskPlanModel(BaseModel):
    tasks: list[dict[str, Any]]


PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["respond", "ask", "act", "continue", "end"]},
        "message": {"type": "string"},
        "question": {"type": "string"},
        "missing": {"type": "string"},
        "capability": {"type": "string"},
        "operation": {"type": "string"},
        "arguments": {"type": "object"},
        "purpose": {"type": "string"},
        "status": {"type": "string", "enum": ["completed", "cancelled", "abandoned"]},
        "summary": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["kind"],
}

TASK_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "domain_name": {"type": ["string", "null"]},
                    "project_name": {"type": ["string", "null"]},
                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["goal"],
            },
        }
    },
    "required": ["tasks"],
}


def parse_proposal(raw: str) -> Proposal:
    data = json.loads(raw)
    if not isinstance(data, dict) or "kind" not in data:
        raise ValueError("proposal must be a JSON object with kind")
    kind = data["kind"]
    if kind == "respond":
        m = RespondModel.model_validate(data)
        return RespondAction(message=m.message, reason=m.reason)
    if kind == "ask":
        m = AskModel.model_validate(data)
        return AskAction(question=m.question, missing=m.missing, reason=m.reason)
    if kind == "act":
        m = ActModel.model_validate(data)
        return ActAction(capability=m.capability, operation=m.operation, arguments=m.arguments, reason=m.reason)
    if kind == "continue":
        m = ContinueModel.model_validate(data)
        return ContinueAction(purpose=m.purpose, reason=m.reason)
    if kind == "end":
        m = EndModel.model_validate(data)
        if m.status not in {"completed", "cancelled", "abandoned"}:
            raise ValueError("invalid end status")
        return EndAction(status=m.status, summary=m.summary, reason=m.reason)  # type: ignore[arg-type]
    raise ValueError(f"unknown kind {kind}")


def parse_task_plan(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
        plan = TaskPlanModel.model_validate(data)
        return plan.tasks
    except (json.JSONDecodeError, ValidationError):
        return []

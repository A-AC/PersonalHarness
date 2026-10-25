from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

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


# Loose schema: the V1 shape. It constrains only the kind and leaves per-kind
# required fields to the parser, so {"kind":"act"} is a valid completion.
PROPOSAL_SCHEMA_LOOSE = {
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


def _variant(kind: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"kind": {"const": kind}, **properties},
        "required": ["kind", *required],
        "additionalProperties": False,
    }


# Strict schema: one branch per action kind. This is what constrained decoding
# needs to prevent a syntactically valid but semantically incomplete action.
PROPOSAL_SCHEMA_STRICT = {
    "type": "object",
    "oneOf": [
        _variant("respond", {"message": {"type": "string"}, "reason": {"type": "string"}}, ["message"]),
        _variant(
            "ask",
            {
                "question": {"type": "string"},
                "missing": {"type": "string"},
                "reason": {"type": "string"},
            },
            ["question", "missing"],
        ),
        _variant(
            "act",
            {
                "capability": {"type": "string"},
                "operation": {"type": "string"},
                "arguments": {"type": "object"},
                "reason": {"type": "string"},
            },
            ["capability", "operation"],
        ),
        _variant("continue", {"purpose": {"type": "string"}, "reason": {"type": "string"}}, ["purpose"]),
        _variant(
            "end",
            {
                "status": {"type": "string", "enum": ["completed", "cancelled", "abandoned"]},
                "summary": {"type": "string"},
                "reason": {"type": "string"},
            },
            ["status", "summary"],
        ),
    ],
}

PROPOSAL_SCHEMA = PROPOSAL_SCHEMA_LOOSE

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


def _validate(model: type[BaseModel], data: dict[str, Any]) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = []
        for item in exc.errors():
            location = ".".join(str(part) for part in item.get("loc") or ()) or "proposal"
            problems.append(f"{location}: {item.get('msg')}")
        raise ValueError("; ".join(problems)) from exc


def parse_proposal(raw: str) -> Proposal:
    data = json.loads(raw)
    if not isinstance(data, dict) or "kind" not in data:
        raise ValueError("proposal must be a JSON object with kind")
    kind = data["kind"]
    if kind == "respond":
        m = _validate(RespondModel, data)
        return RespondAction(message=m.message, reason=m.reason)
    if kind == "ask":
        m = _validate(AskModel, data)
        return AskAction(question=m.question, missing=m.missing, reason=m.reason)
    if kind == "act":
        m = _validate(ActModel, data)
        return ActAction(capability=m.capability, operation=m.operation, arguments=m.arguments, reason=m.reason)
    if kind == "continue":
        m = _validate(ContinueModel, data)
        return ContinueAction(purpose=m.purpose, reason=m.reason)
    if kind == "end":
        m = _validate(EndModel, data)
        if m.status not in {"completed", "cancelled", "abandoned"}:
            raise ValueError(f'invalid end.status "{m.status}"; allowed: completed, cancelled, abandoned')
        return EndAction(status=m.status, summary=m.summary, reason=m.reason)  # type: ignore[arg-type]
    raise ValueError(f'unknown kind "{kind}"; allowed: respond, ask, act, continue, end')


def parse_task_plan(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
        plan = TaskPlanModel.model_validate(data)
        return plan.tasks
    except (json.JSONDecodeError, ValidationError):
        return []

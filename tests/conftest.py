from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.app import Harness, build_harness
from harness.model.scripted import ScriptedAdapter


def plan(*goals: str, project: str | None = None) -> dict[str, Any]:
    return {
        "tasks": [
            {
                "goal": goal,
                "domain_name": None,
                "project_name": project,
                "depends_on": [],
            }
            for goal in goals
        ]
    }


def act(capability: str, operation: str, **arguments: Any) -> dict[str, Any]:
    return {"kind": "act", "capability": capability, "operation": operation, "arguments": arguments}


def respond(message: str) -> dict[str, Any]:
    return {"kind": "respond", "message": message}


def ask(question: str, missing: str) -> dict[str, Any]:
    return {"kind": "ask", "question": question, "missing": missing}


def end(status: str = "cancelled", summary: str = "") -> dict[str, Any]:
    return {"kind": "end", "status": status, "summary": summary}


def write_settings(root: Path, payload: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8", newline="\n")


@pytest.fixture
def make_harness(tmp_path: Path):
    def factory(script: list[Any] | None = None, settings: dict[str, Any] | None = None) -> Harness:
        if settings:
            write_settings(tmp_path, settings)
        model = ScriptedAdapter(script or [])
        return build_harness(tmp_path, model=model)

    return factory


@pytest.fixture
def harness(make_harness) -> Harness:
    return make_harness(
        [
            plan("hello"),
            respond("Hello."),
        ]
    )


def dump(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)

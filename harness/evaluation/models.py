"""Serializable value objects for scenarios, steps, assertions, and reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MODES = ("deterministic", "live")
STEP_KINDS = {
    "turn",
    "inspect",
    "tick",
    "inbox",
    "approve",
    "reject",
    "cancel",
    "restart",
    "schedule_due",
    "sweep_memory",
    "sleep",
    "set_config",
    "create_task",
    "create_schedule",
    "create_memory",
    "archive_memory",
    "restore_memory",
    "module_status",
}
STATUSES = ("passed", "failed", "skipped", "error")


@dataclass
class ScenarioStep:
    """One ordered action inside a scenario.

    ``script`` is used only in deterministic mode: it is the list handed to
    :class:`harness.model.ScriptedModel` for that turn. Every other field is a
    step option (inbox payload, approval selector, time shift, ...).
    """

    kind: str
    name: str = ""
    input: str = ""
    script: list[Any] | None = None
    expect: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    def option(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "input": self.input,
            "script": self.script,
            "expect": self.expect,
            "options": self.options,
        }


@dataclass
class Scenario:
    id: str
    name: str
    category: str
    severity: str = "medium"
    description: str = ""
    modes: list[str] = field(default_factory=lambda: ["deterministic"])
    tags: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    fixtures: dict[str, Any] = field(default_factory=dict)
    steps: list[ScenarioStep] = field(default_factory=list)
    cleanup: str = "reset"
    source: str = ""

    def supports(self, mode: str) -> bool:
        return mode in self.modes

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "severity": self.severity,
            "description": self.description,
            "modes": list(self.modes),
            "tags": list(self.tags),
            "settings": self.settings,
            "fixtures": self.fixtures,
            "cleanup": self.cleanup,
            "source": self.source,
            "steps": [step.as_dict() for step in self.steps],
        }


@dataclass
class AssertionResult:
    id: str
    check: str
    status: str
    expected: Any = None
    observed: Any = None
    message: str = ""
    severity: str = "medium"
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "check": self.check,
            "status": self.status,
            "expected": self.expected,
            "observed": self.observed,
            "message": self.message,
            "severity": self.severity,
            "evidence": self.evidence,
        }


@dataclass
class StepResult:
    index: int
    kind: str
    name: str = ""
    input: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    status: str = "passed"
    response: str = ""
    run_status: str = ""
    task_id: str = ""
    run_id: str = ""
    error: str | None = None
    assertions: list[AssertionResult] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "name": self.name,
            "input": self.input,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "response": self.response,
            "run_status": self.run_status,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "error": self.error,
            "assertions": [item.as_dict() for item in self.assertions],
            "evidence": self.evidence,
        }


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    category: str
    severity: str
    mode: str
    repeat: int
    run_id: str
    status: str = "passed"
    duration_ms: int = 0
    steps: list[StepResult] = field(default_factory=list)
    error: str | None = None
    data_dir: str = ""

    @property
    def assertions(self) -> list[AssertionResult]:
        return [a for step in self.steps for a in step.assertions]

    @property
    def critical_failures(self) -> list[AssertionResult]:
        return [
            a for a in self.assertions
            if a.status == "failed" and a.severity == "critical"
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "category": self.category,
            "severity": self.severity,
            "mode": self.mode,
            "repeat": self.repeat,
            "run_id": self.run_id,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "data_dir": self.data_dir,
            "steps": [step.as_dict() for step in self.steps],
            "assertions": [item.as_dict() for item in self.assertions],
        }


@dataclass
class EvaluationRun:
    run_id: str
    mode: str
    tag: str
    started_at: str
    finished_at: str = ""
    model: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    scenarios: list[ScenarioResult] = field(default_factory=list)
    endpoint: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    results_path: str = ""
    report_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "format_version": 1,
            "run_id": self.run_id,
            "mode": self.mode,
            "tag": self.tag,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "model": self.model,
            "config": self.config,
            "endpoint": self.endpoint,
            "notes": list(self.notes),
            "results_path": self.results_path,
            "report_path": self.report_path,
            "scenarios": [item.as_dict() for item in self.scenarios],
            "metrics": metrics(self),
        }


def metrics(run: EvaluationRun) -> dict[str, Any]:
    """Counts with real denominators, per scenario and per assertion."""
    scenarios = run.scenarios
    assertions = [a for item in scenarios for a in item.assertions]
    by_status: dict[str, int] = {}
    for item in scenarios:
        by_status[item.status] = by_status.get(item.status, 0) + 1
    assertion_status: dict[str, int] = {}
    for item in assertions:
        assertion_status[item.status] = assertion_status.get(item.status, 0) + 1
    categories: dict[str, dict[str, int]] = {}
    for item in scenarios:
        bucket = categories.setdefault(
            item.category, {"scenarios": 0, "passed": 0, "assertions": 0,
                            "assertions_passed": 0},
        )
        bucket["scenarios"] += 1
        if item.status == "passed":
            bucket["passed"] += 1
        bucket["assertions"] += len(item.assertions)
        bucket["assertions_passed"] += sum(
            1 for a in item.assertions if a.status == "passed"
        )
    failures = [
        {
            "scenario_id": item.scenario_id,
            "repeat": item.repeat,
            "step": step.name or f"#{step.index}",
            "assertion": assertion.id,
            "expected": assertion.expected,
            "observed": assertion.observed,
            "message": assertion.message,
            "severity": assertion.severity,
        }
        for item in scenarios
        for step in item.steps
        for assertion in step.assertions
        if assertion.status == "failed"
    ]
    return {
        "scenarios": len(scenarios),
        "scenarios_by_status": by_status,
        "scenario_pass_rate": _rate(by_status.get("passed", 0), len(scenarios)),
        "assertions": len(assertions),
        "assertions_by_status": assertion_status,
        "assertion_pass_rate": _rate(
            assertion_status.get("passed", 0), len(assertions)
        ),
        "critical_failures": len([
            a for a in assertions
            if a.status == "failed" and a.severity == "critical"
        ]),
        "categories": categories,
        "failures": failures,
    }


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": round(numerator / denominator, 4) if denominator else None,
    }

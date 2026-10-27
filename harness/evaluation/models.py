from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness.app import Harness
from harness.contracts.actions import ExecutionResult
from harness.contracts.decisions import Decision
from harness.contracts.domains import Domain, Suggestion
from harness.contracts.loop import TurnResult
from harness.contracts.memory import MemoryRecord
from harness.contracts.tasks import Task
from harness.contracts.traces import Trace


STATUS_ORDER = {"skipped": 0, "inconclusive": 1, "failed": 2}


@dataclass(frozen=True)
class AssertionSpec:
    """One declared check.

    ``type`` names a built-in assertion from
    :mod:`harness.evaluation.assertions`. ``config`` carries its arguments. An
    assertion can be tagged with a metric so reports can show a denominator and
    a numerator rather than a single aggregate score.
    """

    id: str
    type: str
    config: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    metric: str | None = None
    required: bool = True
    severity: str = ""


@dataclass
class AssertionResult:
    id: str
    type: str
    description: str
    status: str
    expected: Any = None
    observed: Any = None
    message: str = ""
    metric: str | None = None
    required: bool = True
    severity: str = "medium"
    evidence_refs: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "description": self.description,
            "status": self.status,
            "expected": self.expected,
            "observed": self.observed,
            "message": self.message,
            "metric": self.metric,
            "required": self.required,
            "severity": self.severity,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass
class ScenarioStep:
    kind: str = "turn"
    input: str | None = None
    script: list[Any] | None = None
    expect: dict[str, Any] = field(default_factory=dict)
    ticks: int = 1
    seconds: float = 0.0
    name: str | None = None
    action: str | None = None
    target: str | None = None


@dataclass
class Scenario:
    id: str
    name: str
    category: str
    severity: str
    description: str = ""
    modes: list[str] = field(default_factory=lambda: ["deterministic"])
    tags: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    fixtures: dict[str, Any] = field(default_factory=dict)
    steps: list[ScenarioStep] = field(default_factory=list)
    cleanup: str = "retain"
    source: str = ""

    def supports(self, mode: str) -> bool:
        return mode in self.modes

    @property
    def deterministic(self) -> bool:
        return self.supports("deterministic") and all(
            step.script is not None for step in self.steps if step.kind == "turn"
        )


@dataclass
class StepObservation:
    harness: Harness
    scenario: Scenario
    step_index: int
    step: ScenarioStep
    started_at: str
    finished_at: str
    duration_ms: int
    response: str = ""
    turn_result: TurnResult | None = None
    error: str | None = None
    tasks_before: list[Task] = field(default_factory=list)
    tasks_after: list[Task] = field(default_factory=list)
    new_tasks: list[Task] = field(default_factory=list)
    decisions_before: list[Decision] = field(default_factory=list)
    decisions_after: list[Decision] = field(default_factory=list)
    new_decisions: list[Decision] = field(default_factory=list)
    attempts: list[ExecutionResult] = field(default_factory=list)
    all_attempts: list[ExecutionResult] = field(default_factory=list)
    memories_before: list[MemoryRecord] = field(default_factory=list)
    memories_after: list[MemoryRecord] = field(default_factory=list)
    domains_after: list[Domain] = field(default_factory=list)
    suggestions_after: list[Suggestion] = field(default_factory=list)
    traces: list[Trace] = field(default_factory=list)
    events: list[Any] = field(default_factory=list)
    tick_result: Any = None
    restart: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)
    interaction_id: str = ""
    interaction_task_ids: list[str] = field(default_factory=list)
    fixture_refs: dict[str, str] = field(default_factory=dict)

    def task_by_goal(self, text: str) -> list[Task]:
        wanted = text.lower()
        return [item for item in self.tasks_after if wanted in item.goal.lower()]

    def memory_by_content(self, text: str) -> list[MemoryRecord]:
        wanted = text.lower()
        return [item for item in self.memories_after if wanted in item.content.lower()]

    def domain_by_name(self, name: str) -> Domain | None:
        wanted = name.strip().lower()
        for item in self.domains_after:
            if item.name.lower() == wanted:
                return item
        return None


@dataclass
class ScenarioRun:
    scenario_id: str
    scenario_name: str
    category: str
    severity: str
    mode: str
    repeat: int
    run_id: str
    status: str = "inconclusive"
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    steps: list[dict[str, Any]] = field(default_factory=list)
    assertions: list[AssertionResult] = field(default_factory=list)
    error: str | None = None
    run_dir: str = ""
    fixture_ids: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def critical_failures(self) -> list[AssertionResult]:
        return [
            item
            for item in self.assertions
            if item.status == "failed" and item.severity == "critical"
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "category": self.category,
            "severity": self.severity,
            "mode": self.mode,
            "repeat": self.repeat,
            "run_id": self.run_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "run_dir": self.run_dir,
            "fixture_ids": self.fixture_ids,
            "evidence": self.evidence,
            "steps": self.steps,
            "assertions": [item.as_dict() for item in self.assertions],
        }


@dataclass
class EvaluationRun:
    run_id: str
    mode: str
    tag: str
    started_at: str
    finished_at: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    scenarios: list[ScenarioRun] = field(default_factory=list)
    sandbox_root: str = ""
    results_dir: str = ""
    notes: list[str] = field(default_factory=list)
    endpoint_ok: bool | None = None
    endpoint_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "format_version": 1,
            "run_id": self.run_id,
            "mode": self.mode,
            "tag": self.tag,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "config": self.config,
            "sandbox_root": self.sandbox_root,
            "results_dir": self.results_dir,
            "endpoint_ok": self.endpoint_ok,
            "endpoint_error": self.endpoint_error,
            "notes": list(self.notes),
            "scenarios": [item.as_dict() for item in self.scenarios],
        }

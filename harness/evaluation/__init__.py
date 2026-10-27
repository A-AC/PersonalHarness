"""Scenario-driven evaluation for the personal assistant harness.

Two modes share one scenario format:

* ``deterministic`` drives the real managers, control plane, executor,
  scheduler, memory system, and learning lifecycle with a scripted model.
* ``live`` drives the same code with the configured model adapter and endpoint.

The evaluator never treats a plausible model response as evidence. Assertions
inspect persisted tasks, decisions, memories, domains, execution attempts, and
trace context inclusion.
"""

from harness.evaluation.models import (
    AssertionResult,
    AssertionSpec,
    EvaluationRun,
    Scenario,
    ScenarioRun,
    ScenarioStep,
    StepObservation,
)
from harness.evaluation.loader import select_scenarios
from harness.evaluation.runner import run_evaluation

__all__ = [
    "AssertionResult",
    "AssertionSpec",
    "EvaluationRun",
    "Scenario",
    "ScenarioRun",
    "ScenarioStep",
    "StepObservation",
    "run_evaluation",
    "select_scenarios",
]

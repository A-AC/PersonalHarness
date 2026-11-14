"""Scenario evaluation for the current harness architecture.

The suite runs real scenarios against a real :class:`harness.app.App` in a
throwaway data directory. It never touches the deployed data directory and
never mocks the managers: tasks, runs, memory, approvals, schedules, inbox,
modules, skills, and sub-agents are the production implementations.

Two modes:

``deterministic``
    A :class:`harness.model.ScriptedModel` stands in for the endpoint. The
    suite proves harness behavior (permissions, persistence, recovery,
    scheduling, retrieval, and multi-tool loops) without network access.

``live``
    The configured OpenAI-compatible endpoint is used. The model can be
    replaced per run with ``--model``/``--base-url``/``--api-key`` (or the
    ``HARNESS_MODEL_*`` environment variables). The endpoint is probed first
    and the run refuses to start if the requested model is not served, so a
    live report can never silently describe a different model. Live scenarios
    range from smoke checks to a behavioral suite with separate rubric scoring
    for open-ended replies.
"""

from __future__ import annotations

from .aggregate import aggregate_reports, render_aggregate
from .loader import load_scenarios, select_scenarios
from .models import (
    AssertionResult,
    EvaluationRun,
    Scenario,
    ScenarioResult,
    ScenarioStep,
    StepResult,
)
from .runner import EvaluationError, EndpointUnavailable, Evaluator, run_evaluation

__all__ = [
    "AssertionResult",
    "EndpointUnavailable",
    "EvaluationError",
    "EvaluationRun",
    "Evaluator",
    "Scenario",
    "ScenarioResult",
    "ScenarioStep",
    "StepResult",
    "aggregate_reports",
    "load_scenarios",
    "render_aggregate",
    "run_evaluation",
    "select_scenarios",
]

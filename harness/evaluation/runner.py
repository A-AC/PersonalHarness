"""Execution engine for deterministic and live evaluation modes."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import yaml

from harness.app import (
    Harness,
    build_configured_model,
    build_harness,
    build_scripted_model,
    build_settings_service,
    is_scripted_model,
)
from harness.contracts.learning import LearningSignal, SkillDraft
from harness.contracts.settings import SettingsService
from harness.contracts.tasks import Task
from harness.evaluation.assertions import (
    compile_expectation,
    evaluate_assertions,
    status_for_assertions,
)
from harness.evaluation.fixtures import apply_fixtures
from harness.evaluation.loader import (
    Scenario,
    load_default_scenarios,
    select_scenarios as select_loaded,
)
from harness.evaluation.models import (
    AssertionResult,
    EvaluationRun,
    ScenarioRun,
    ScenarioStep,
    StepObservation,
)
from harness.evaluation.safety import default_sandbox_root, prepare_sandbox
from harness.storage.atomic import now_ts


BASELINE_SETTINGS: dict[str, Any] = {
    "tasks": {"planning_mode": "llm"},
    "loop": {"stop_after_nonretryable_failure": False},
    "scheduler": {
        "max_tasks_per_tick": 0,
        "retry_cooldown_s": 0.0,
    },
    # Automatic suggestions make unrelated scenarios noisy. A scenario that
    # tests suggestions overrides this threshold explicitly. The catalog caps
    # the value at 50, so this is the quietest legal setting.
    "domain": {"suggestion": {"threshold": 50}},
    "context": {
        "history": {
            "strategy": "full",
            "summaries": False,
        }
    },
}
SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|secret|password|authorization|bearer|(?<!max_)(?<!budget_)token)",
    re.IGNORECASE,
)
SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")


class EvaluationError(RuntimeError):
    pass


class EndpointUnavailable(EvaluationError):
    pass


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
    return target


def _nested(flat: dict[str, Any]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for key, value in flat.items():
        parts = key.split(".")
        cursor = tree
        for part in parts[:-1]:
            child = cursor.get(part)
            if not isinstance(child, dict):
                child = {}
                cursor[part] = child
            cursor = child
        cursor[parts[-1]] = copy.deepcopy(value)
    return tree


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten(value, full))
        else:
            flat[full] = value
    return flat


def _write_settings(path: Path, values: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(values, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )


def _source_settings(sandbox: Path, config_from: str | Path | None, *, env: dict[str, str]) -> dict[str, Any]:
    """Read the deployed configuration without mutating it.

    A non-existent source directory is normal: catalog defaults plus runtime
    environment variables still identify the model. The source file is copied
    into the sandbox before the settings service is allowed to migrate or
    persist anything.
    """

    scratch = sandbox / "_config_source"
    scratch.mkdir(parents=True, exist_ok=True)
    source_dir = Path(config_from) if config_from is not None else Path("./data")
    source_file = source_dir / "settings.yaml"
    copy_file = scratch / "settings.yaml"
    if source_file.exists() and source_file.is_file():
        copy_file.write_text(source_file.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    service = build_settings_service(copy_file, env=env)
    return service.as_dict()


def scenario_settings(
    source_values: dict[str, Any],
    scenario: Scenario,
) -> dict[str, Any]:
    values = _nested(dict(source_values))
    _deep_merge(values, copy.deepcopy(BASELINE_SETTINGS))
    # Scenario keys may be nested (normal YAML) or dotted (compact overrides).
    flat = _flatten(scenario.settings)
    nested_scenario = _nested(flat)
    _deep_merge(values, nested_scenario)
    return values


def _config_snapshot(settings: SettingsService) -> dict[str, Any]:
    return {
        "model": settings.get("models.interactive.model_name"),
        "endpoint": settings.get("models.interactive.endpoint"),
        "response_format": settings.get("models.interactive.response_format"),
        "action_schema": settings.get("models.interactive.action_schema"),
        "temperature": settings.get("models.interactive.temperature"),
        "max_tokens": settings.get("models.interactive.max_tokens"),
        "seed": settings.get("models.interactive.seed"),
        "stream": settings.get("models.interactive.stream"),
        "thinking_budget_tokens": settings.get("models.interactive.thinking_budget_tokens"),
        "enable_thinking": settings.get("models.interactive.enable_thinking"),
        "timeout_s": settings.get("models.interactive.timeout_s"),
        "prompt_variant": settings.get("context.prompt_variant"),
        "history_strategy": settings.get("context.history.strategy"),
        "history_budget": settings.get("context.budgets.history"),
        "planning_mode": settings.get("tasks.planning_mode"),
        "scheduler_max_tasks_per_tick": settings.get("scheduler.max_tasks_per_tick"),
        "capabilities_policy": {
            "read": settings.get("capabilities.policy.read"),
            "write": settings.get("capabilities.policy.write"),
            "destructive": settings.get("capabilities.policy.destructive"),
        },
        "memory_retrieval_k": settings.get("memory.retrieval.k"),
        "domain_inference_enabled": settings.get("domain.inference.enabled"),
    }


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                result[key] = "[redacted]"
            else:
                result[key] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _task_row(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "goal": task.goal,
        "state": task.state,
        "domain_id": task.domain_id,
        "block_reason": task.block_reason,
        "depends_on": list(task.depends_on),
        "interaction_id": task.interaction_id,
        "last_message": task.last_message,
        "outcome": task.outcome.summary if task.outcome is not None else None,
        "progress": list(task.progress),
        "updated_at": task.updated_at,
    }


def _all_attempts(harness: Harness) -> list[Any]:
    grouped = harness.capabilities.attempts_by_task()
    items = [item for values in grouped.values() for item in values]
    items.sort(
        key=lambda item: (
            (item.evidence or {}).get("started_at", ""),
            (item.evidence or {}).get("sequence", 0),
        )
    )
    return items


def _attempt_row(item: Any) -> dict[str, Any]:
    return {
        "operation_id": item.operation_id,
        "task_id": item.task_id,
        "capability": item.capability,
        "operation": item.operation,
        "status": item.status,
        "data": item.data,
        "message": item.message,
        "error_category": item.error_category,
    }


def _decision_row(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "status": item.status,
        "summary": item.summary,
        "proposal": item.proposal,
        "task_ids": item.task_ids,
        "domain_id": item.domain_id,
        "resolution": item.resolution,
        "consumed_at": item.consumed_at,
        "operation_id": item.operation_id,
    }


def _memory_row(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "type": item.type,
        "kind": item.kind,
        "content": item.content,
        "scope": item.scope,
        "status": item.status,
        "supersedes": item.supersedes,
        "superseded_by": item.superseded_by,
    }


def _trace_row(trace: Any) -> dict[str, Any]:
    return {
        "id": trace.id,
        "role": trace.role,
        "task_id": trace.task_id,
        "model_called": trace.model_called,
        "duration_ms": trace.duration_ms if trace.duration_ms is not None else trace.latency_ms,
        "time_to_first_token_ms": trace.time_to_first_token_ms,
        "prompt_tokens": trace.prompt_tokens,
        "completion_tokens": trace.completion_tokens,
        "validation": trace.validation,
        "included": list(trace.included),
        "excluded": list(trace.excluded),
    }


def _step_by_kind(step: ScenarioStep) -> str:
    return step.kind


def _flatten_scripts(scenario: Scenario) -> list[Any]:
    scripts: list[Any] = []
    for step in scenario.steps:
        if step.script is not None:
            scripts.extend(copy.deepcopy(step.script))
    return scripts


def _fixture_lookup(refs: dict[str, Any], token: str) -> Any:
    kind, _, key = token.partition(":")
    if kind in {"domain", "domain_id"}:
        values = refs.get("domains") or {}
        target = key.lower()
        for name, value in values.items():
            if name.lower() == target:
                return value
        return None
    if kind == "memory":
        values = refs.get("memories") or {}
        return values.get(key)
    if kind == "task":
        values = refs.get("tasks") or {}
        return values.get(key)
    if kind == "decision":
        values = refs.get("decisions") or {}
        return values.get(key)
    if kind == "skill":
        values = refs.get("skills") or {}
        target = key.lower()
        for name, value in values.items():
            if name.lower() == target:
                return value
        return None
    if kind in {"interaction", "interaction_id"}:
        values = refs.get("interactions") or []
        try:
            return values[int(key)]
        except (ValueError, IndexError):
            return None
    if kind == "active_domain":
        return refs.get("active_domain")
    return None


def _interpolate(value: Any, refs: dict[str, Any]) -> Any:
    """Resolve ``$domain:Garden``-style fixture references in scripted payloads.

    Only exact reference strings are substituted. Embedded text is left alone
    so ordinary prose containing a dollar sign is never rewritten.
    """

    if isinstance(value, dict):
        return {key: _interpolate(item, refs) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate(item, refs) for item in value]
    if isinstance(value, str) and value.startswith("$"):
        resolved = _fixture_lookup(refs, value[1:])
        if resolved is not None:
            return resolved
    return value


def _region_invariant(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _region_invariant(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_region_invariant(item) for item in value]
    return value


def _new_events(harness: Harness, seen: set[str]) -> list[Any]:
    events = harness.events.query(limit=1000)
    fresh = [item for item in events if item.id not in seen]
    for item in fresh:
        seen.add(item.id)
    return fresh


def _build_observation(
    *,
    harness: Harness,
    scenario: Scenario,
    step: ScenarioStep,
    step_index: int,
    started_at: str,
    started_monotonic: float,
    before: dict[str, Any],
    seen_attempts: set[str],
    seen_events: set[str],
    result: Any,
    tick_result: Any = None,
    restart: bool = False,
) -> StepObservation:
    tasks_after = harness.tasks.list()
    decisions_after = harness.decisions.list(include_resolved=True)
    memories_after = harness.memory.list()
    all_attempts = _all_attempts(harness)
    new_attempts = [item for item in all_attempts if item.operation_id not in seen_attempts]
    seen_attempts.update(item.operation_id for item in new_attempts)
    events = _new_events(harness, seen_events)
    interaction_id = getattr(result, "interaction_id", "") if result is not None else ""
    traces = (
        harness.traces.list_for(interaction_id=interaction_id, limit=200)
        if interaction_id
        else []
    )
    before_task_ids = {item.id for item in before["tasks"]}
    before_decision_ids = {item.id for item in before["decisions"]}
    turn_tasks = [
        item for item in tasks_after if item.interaction_id and item.interaction_id == interaction_id
    ]
    response = "\n".join(getattr(result, "messages", []) or []) if result is not None else ""
    obs = StepObservation(
        harness=harness,
        scenario=scenario,
        step_index=step_index,
        step=step,
        started_at=started_at,
        finished_at=now_ts(),
        duration_ms=int((time.monotonic() - started_monotonic) * 1000),
        response=response,
        turn_result=result if step.kind == "turn" else None,
        error=getattr(result, "error", None) if result is not None else None,
        tasks_before=list(before["tasks"]),
        tasks_after=tasks_after,
        new_tasks=[item for item in tasks_after if item.id not in before_task_ids],
        decisions_before=list(before["decisions"]),
        decisions_after=decisions_after,
        new_decisions=[item for item in decisions_after if item.id not in before_decision_ids],
        attempts=new_attempts,
        all_attempts=all_attempts,
        memories_before=list(before["memories"]),
        memories_after=memories_after,
        domains_after=harness.domains.list_domains(include_archived=True),
        suggestions_after=harness.domains.list_suggestions(),
        traces=traces,
        events=events,
        tick_result=tick_result,
        restart=restart,
        interaction_id=interaction_id,
        interaction_task_ids=list(getattr(result, "task_ids", []) or []),
        fixture_refs=before.get("fixture_refs") or {},
        evidence={
            "domains_before": before.get("domain_rows"),
            "turn_tasks": [_task_row(item) for item in turn_tasks],
        },
    )
    return obs


def _step_record(obs: StepObservation, assertion_ids: list[str]) -> dict[str, Any]:
    return {
        "index": obs.step_index,
        "kind": obs.step.kind,
        "name": obs.step.name,
        "input": obs.step.input,
        "response": obs.response,
        "duration_ms": obs.duration_ms,
        "error": obs.error,
        "interaction_id": obs.interaction_id,
        "restart": obs.restart,
        "tasks": [_task_row(item) for item in obs.tasks_after],
        "new_tasks": [_task_row(item) for item in obs.new_tasks],
        "decisions": [_decision_row(item) for item in obs.decisions_after],
        "new_decisions": [_decision_row(item) for item in obs.new_decisions],
        "attempts": [_attempt_row(item) for item in obs.attempts],
        "memories": [_memory_row(item) for item in obs.memories_after],
        "domains": [
            {"id": item.id, "name": item.name, "archived": item.archived}
            for item in obs.domains_after
        ],
        "traces": [_trace_row(item) for item in obs.traces],
        "context": {
            "included": [row for trace in obs.traces for row in trace.included],
            "excluded": [row for trace in obs.traces for row in trace.excluded],
        },
        "events": [
            {
                "id": item.id,
                "operation": item.operation,
                "result": item.result,
                "target": item.target,
                "data": item.data,
            }
            for item in obs.events
        ],
        "tick": (
            {
                "ran": int(getattr(obs.tick_result, "ran", 0) or 0),
                "items": [
                    {
                        "kind": getattr(item, "kind", None),
                        "action": getattr(item, "action", None),
                        "task_id": getattr(item, "task_id", None),
                        "reason": getattr(item, "reason", None),
                    }
                    for item in getattr(obs.tick_result, "items", []) or []
                ],
            }
            if obs.tick_result is not None
            else None
        ),
        "assertion_ids": assertion_ids,
    }


def _scenario_evidence(
    harness: Harness,
    scenario: Scenario,
    steps: list[dict[str, Any]],
    fixture_refs: dict[str, Any],
) -> dict[str, Any]:
    return {
        "domain_ids": sorted(
            {item.id for item in harness.domains.list_domains(include_archived=True)}
        ),
        "memory_ids": sorted(item.id for item in harness.memory.list()),
        "task_ids": sorted(item.id for item in harness.tasks.list()),
        "decision_ids": sorted(item.id for item in harness.decisions.list(include_resolved=True)),
        "attempt_ids": sorted(item.operation_id for item in _all_attempts(harness)),
        "trace_ids": sorted(
            {
                trace["id"]
                for step in steps
                for trace in step.get("traces", [])
            }
        ),
        "interaction_ids": sorted(
            {
                step["interaction_id"]
                for step in steps
                if step.get("interaction_id")
            }
        ),
        "fixture_refs": fixture_refs,
    }


def _final_status(
    assertions: list[AssertionResult],
    *,
    error: str | None,
) -> tuple[str, str]:
    if error:
        return "failed", error
    status, reason = status_for_assertions(assertions)
    if status == "failed" and any(
        item.status == "failed" and item.severity == "critical" for item in assertions
    ):
        return "failed", f"critical failure: {reason}"
    return status, reason


def _execute_step(
    harness: Harness,
    scenario: Scenario,
    step: ScenarioStep,
    *,
    env: dict[str, str],
    adapter_factory: Callable[[SettingsService], Any] | None,
) -> tuple[Harness, Any, Any, bool]:
    """Return (harness, turn result, tick result, restarted)."""

    if step.kind == "turn":
        return harness, harness.loop.handle_turn(str(step.input or "")), None, False
    if step.kind == "inspect":
        # A read-only observation step. Domain-content assertions query the
        # inspection service directly, so no state is mutated here.
        return harness, None, None, False
    if step.kind == "tick":
        aggregated_items: list[Any] = []
        ran_total = 0
        for _ in range(max(1, int(step.ticks))):
            report = harness.scheduler.tick()
            aggregated_items.extend(getattr(report, "items", []) or [])
            ran_total += int(getattr(report, "ran", 0) or 0)
        return harness, None, SimpleNamespace(items=aggregated_items, ran=ran_total), False
    if step.kind == "restart":
        if is_scripted_model(harness.model):
            model: Any = harness.model
        elif adapter_factory is not None:
            model = adapter_factory(build_settings_service(harness.data_dir / "settings.yaml", env=env))
        else:
            model = None
        restarted = build_harness(harness.data_dir, env=env, model=model)
        return restarted, None, None, True
    if step.kind == "sleep":
        time.sleep(max(0.0, min(float(step.seconds), 30.0)))
        return harness, None, None, False
    if step.kind == "review":
        return harness, None, harness.learning_review.review(trigger="evaluation"), False
    if step.kind == "learning_signal":
        # Exercise the real evidence gate without inventing a second learning
        # path. The pipeline journals its decision, which assertions inspect.
        harness.learning_pipeline.process(
            LearningSignal(
                kind=str(step.target or "transient_observation"),
                content=str(step.input or ""),
                origin=str(step.action or "harness"),
            )
        )
        return harness, None, None, False
    if step.kind == "skill":
        target = str(step.target or "")
        action = str(step.action or "").lower()
        if action == "propose":
            text = str(step.target or "")
            body = str(step.input or f"# {text}\n\nFollow the recorded procedure.")
            harness.skill_library.propose(
                SkillDraft(
                    name=text,
                    description="Generated by an evaluation step.",
                    body=body,
                    reason="evaluation step",
                    provenance={"source_kind": "evaluation_step"},
                ),
                actor="evaluation",
            )
            return harness, None, None, False
        records = list(harness.skill_library.list())
        if target.lower() in {"candidate", "approved", "retired", "rejected"}:
            records = [item for item in records if item.status == target.lower()]
        elif target.lower() in {"latest", "newest", ""}:
            records = records[-1:]
        else:
            records = [
                item
                for item in records
                if target in {item.id, item.name} or target.lower() in item.name.lower()
            ]
        record = records[0] if records else None
        if record is None:
            raise ValueError(f"skill fixture not found: {target!r}")
        if action == "approve":
            if record.status == "candidate":
                harness.skill_library.record_test(
                    record.id, {"passed": True, "source": "evaluation_step"}, actor="evaluation"
                )
            harness.skill_library.approve(record.id, actor="evaluation")
        elif action == "retire":
            harness.skill_library.retire(record.id, actor="evaluation")
        elif action == "rollback":
            harness.skill_library.rollback(record.id, actor="evaluation")
        elif action == "reject":
            harness.skill_library.reject(record.id, actor="evaluation")
        else:
            raise ValueError(f"unknown skill action {action!r}")
        return harness, None, None, False
    raise ValueError(f"unknown step kind {step.kind!r}")


def _snapshot(harness: Harness, fixture_refs: dict[str, Any]) -> dict[str, Any]:
    return {
        "tasks": harness.tasks.list(),
        "decisions": harness.decisions.list(include_resolved=True),
        "memories": harness.memory.list(),
        "domain_rows": [
            {
                "id": item.id,
                "name": item.name,
                "archived": item.archived,
                "created_reason": item.created_reason,
                "capability_names": list(item.capability_names),
            }
            for item in harness.domains.list_domains(include_archived=True)
        ],
        "fixture_refs": fixture_refs,
    }


def run_scenario(
    scenario: Scenario,
    *,
    mode: str,
    repeat: int,
    run_dir: Path,
    settings_values: dict[str, Any],
    run_id: str,
    adapter_factory: Callable[[SettingsService], Any] | None = None,
    env: dict[str, str] | None = None,
) -> ScenarioRun:
    runtime_env = dict(env or {})
    started_at = now_ts()
    started_monotonic = time.monotonic()
    run = ScenarioRun(
        scenario_id=scenario.id,
        scenario_name=scenario.name,
        category=scenario.category,
        severity=scenario.severity,
        mode=mode,
        repeat=repeat,
        run_id=run_id,
        started_at=started_at,
        run_dir=str(run_dir),
    )
    if run_dir.exists():
        shutil.rmtree(run_dir)
    _write_settings(run_dir / "settings.yaml", settings_values)
    scripted: Any | None = None
    if mode == "deterministic":
        if not scenario.deterministic:
            run.status = "skipped"
            run.finished_at = now_ts()
            run.error = "scenario has no deterministic script for every turn"
            return run
        # The adapter is created empty so fixtures can establish runtime IDs
        # before script arguments are interpolated.
        scripted = build_scripted_model()
        model = scripted
    elif adapter_factory is not None:
        model = adapter_factory(build_settings_service(run_dir / "settings.yaml", env=runtime_env))
    else:
        model = None
    try:
        harness = build_harness(run_dir, env=runtime_env, model=model)
        fixture_refs = apply_fixtures(harness, scenario.fixtures, scenario_id=scenario.id)
        run.fixture_ids = fixture_refs
        if scripted is not None:
            for item in _interpolate(_flatten_scripts(scenario), fixture_refs):
                scripted.push(item)
        seen_attempts = {item.operation_id for item in _all_attempts(harness)}
        seen_events: set[str] = set()
        _new_events(harness, seen_events)
        step_records: list[dict[str, Any]] = []
        for index, step in enumerate(scenario.steps, start=1):
            before = _snapshot(harness, fixture_refs)
            step_started_at = now_ts()
            step_started = time.monotonic()
            try:
                harness, result, tick_result, restarted = _execute_step(
                    harness,
                    scenario,
                    step,
                    env=runtime_env,
                    adapter_factory=adapter_factory,
                )
            except Exception as exc:
                observation = _build_observation(
                    harness=harness,
                    scenario=scenario,
                    step=step,
                    step_index=index,
                    started_at=step_started_at,
                    started_monotonic=step_started,
                    before=before,
                    seen_attempts=seen_attempts,
                    seen_events=seen_events,
                    result=None,
                )
                observation.error = f"{type(exc).__name__}: {exc}"
                run.error = observation.error
                specs = compile_expectation(
                    _interpolate(step.expect, fixture_refs), scenario, index
                )
                run.assertions.extend(evaluate_assertions(specs, observation, scenario=scenario))
                step_records.append(_step_record(observation, [item.id for item in specs]))
                break
            observation = _build_observation(
                harness=harness,
                scenario=scenario,
                step=step,
                step_index=index,
                started_at=step_started_at,
                started_monotonic=step_started,
                before=before,
                seen_attempts=seen_attempts,
                seen_events=seen_events,
                result=result,
                tick_result=tick_result,
                restart=restarted,
            )
            if result is not None and getattr(result, "error", None):
                run.error = str(result.error)
            specs = compile_expectation(
                _interpolate(step.expect, fixture_refs), scenario, index
            )
            results = evaluate_assertions(specs, observation, scenario=scenario)
            run.assertions.extend(results)
            step_records.append(_step_record(observation, [item.id for item in specs]))
            if run.error:
                break
        run.steps = step_records
        run.evidence = _scenario_evidence(harness, scenario, step_records, fixture_refs)
        status, reason = _final_status(run.assertions, error=run.error)
        run.status = status
        if reason:
            run.evidence["status_reason"] = reason
    except Exception as exc:
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        run.finished_at = now_ts()
        run.duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        if scenario.cleanup == "delete":
            try:
                shutil.rmtree(run_dir)
            except OSError:
                pass
    return run


def _probe_live_endpoint(
    sandbox: Path,
    source_values: dict[str, Any],
    scenario: Scenario,
    *,
    env: dict[str, str],
) -> None:
    probe_dir = sandbox / "_endpoint_probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    values = scenario_settings(source_values, scenario)
    _write_settings(probe_dir / "settings.yaml", values)
    settings = build_settings_service(probe_dir / "settings.yaml", env=env)
    adapter = build_configured_model(settings)
    endpoint = str(settings.get("models.interactive.endpoint"))
    model_name = str(settings.get("models.interactive.model_name") or "")
    if not adapter.health("interactive"):
        raise EndpointUnavailable(
            "live model endpoint is unavailable: "
            f"{endpoint} (model {model_name})"
        )
    advertised = adapter.model_ids("interactive")
    if advertised is not None and model_name not in advertised:
        raise EndpointUnavailable(
            f"configured live model {model_name!r} is not advertised by {endpoint}; "
            f"available models: {', '.join(advertised)}. "
            "Set HARNESS_INTERACTIVE_MODEL or fix the configured alias; "
            "the evaluator will not silently substitute another model."
        )


def run_evaluation(
    *,
    mode: str,
    scenarios: list[Scenario] | None = None,
    scenario_ids: str | None = None,
    categories: str | None = None,
    tags: str | None = None,
    repeats: int = 1,
    tag: str | None = None,
    sandbox_dir: str | Path | None = None,
    config_from: str | Path | None = None,
    adapter_factory: Callable[[SettingsService], Any] | None = None,
    env: dict[str, str] | None = None,
    scenario_dir: str | Path | None = None,
    results_dir: str | Path | None = None,
) -> EvaluationRun:
    if mode not in {"deterministic", "live"}:
        raise ValueError("mode must be 'deterministic' or 'live'")
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    runtime_env = dict(env if env is not None else os.environ)
    source = list(scenarios) if scenarios is not None else load_default_scenarios(scenario_dir)
    selected = select_loaded(
        source,
        scenario_ids=scenario_ids,
        categories=categories,
        tags=tags,
        mode=mode,
    )
    if not selected:
        raise EvaluationError(
            "no scenarios matched the selection for mode "
            f"{mode!r}; list scenarios with --list to see modes"
        )
    sandbox = prepare_sandbox(sandbox_dir or default_sandbox_root())
    run_id = f"eval_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(3)}"
    if tag is None:
        tag = run_id
    if not SAFE_TAG_RE.match(tag):
        raise EvaluationError(
            "tag must be a short filename-safe identifier (letters, digits, '.', '_', '-')"
        )
    source_values = _source_settings(sandbox, config_from, env=runtime_env)
    started_at = now_ts()
    run = EvaluationRun(
        run_id=run_id,
        mode=mode,
        tag=tag,
        started_at=started_at,
        sandbox_root=str(sandbox),
    )
    if mode == "live" and adapter_factory is None:
        try:
            _probe_live_endpoint(sandbox, source_values, selected[0], env=runtime_env)
        except EndpointUnavailable as exc:
            run.endpoint_ok = False
            run.endpoint_error = str(exc)
            raise
        run.endpoint_ok = True
    first_settings: SettingsService | None = None
    for scenario in selected:
        values = scenario_settings(source_values, scenario)
        for repeat in range(1, repeats + 1):
            run_dir = sandbox / "runs" / tag / scenario.id / f"repeat_{repeat}"
            result = run_scenario(
                scenario,
                mode=mode,
                repeat=repeat,
                run_dir=run_dir,
                settings_values=values,
                run_id=run_id,
                adapter_factory=adapter_factory,
                env=runtime_env,
            )
            if first_settings is None:
                try:
                    first_settings = build_settings_service(run_dir / "settings.yaml", env=runtime_env)
                except Exception:
                    first_settings = None
            run.scenarios.append(result)
    if first_settings is not None:
        run.config = _redact(_config_snapshot(first_settings))
        run.config["scenario_defaults"] = {
            "planning_mode": BASELINE_SETTINGS["tasks"]["planning_mode"],
            "scheduler_max_tasks_per_tick": BASELINE_SETTINGS["scheduler"]["max_tasks_per_tick"],
        }
    run.finished_at = now_ts()
    if results_dir is not None:
        from harness.evaluation.metrics import summarize
        from harness.evaluation.report import write_reports

        write_reports(run, summarize(run), results_dir)
    return run


def _rate_text(block: Any) -> str:
    """Render a rate dict (numerator/denominator/rate) without hiding the sample."""
    if not isinstance(block, dict):
        return "n/a"
    value = block.get("rate")
    numerator = block.get("numerator")
    denominator = block.get("denominator")
    if value is None:
        return f"n/a (0/{denominator if denominator is not None else 0})"
    return f"{value:.1%} ({numerator}/{denominator})"


def render_terminal_summary(run: EvaluationRun, summary: dict[str, Any]) -> str:
    lines = [
        f"Evaluation {run.tag} ({run.mode})",
        f"Run ID: {run.run_id}",
        f"Sandbox: {run.sandbox_root}",
        f"Model: {run.config.get('model')} @ {run.config.get('endpoint')}",
        f"Scenarios: {summary['scenario_runs']} run(s), {summary['passed']} passed, "
        f"{summary['failed']} failed, {summary['inconclusive']} inconclusive, "
        f"{summary['skipped']} skipped",
        f"Scenario pass rate: {_rate_text(summary['scenario_pass_rate'])}",
        f"Assertions: {summary['assertions_total']} run, "
        f"{_rate_text(summary['assertion_pass_rate'])} passed",
        f"Critical failures: {summary['critical_failures']}",
        f"Unauthorized dispatches: {summary['unauthorized_dispatch']}",
        f"Latency p50/p95: {summary['latency_p50_ms']} / {summary['latency_p95_ms']} ms",
    ]
    if summary["failed"]:
        lines.append("Failures:")
        for item in summary["failure_rows"][:20]:
            lines.append(
                f"  {item['scenario_id']} #{item['repeat']} [{item['status']}]: {item['reason']}"
            )
    return "\n".join(lines)


__all__ = [
    "BASELINE_SETTINGS",
    "EndpointUnavailable",
    "EvaluationError",
    "run_evaluation",
    "run_scenario",
    "render_terminal_summary",
]

"""Aggregate evaluation results without hiding failures behind one score.

Every rate in this module carries its numerator, denominator, and sample count.
Missing denominators produce ``None`` rather than a misleading zero. Safety
counts are reported separately and never folded into an overall score.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from harness.evaluation.models import AssertionResult, EvaluationRun, ScenarioRun

# Metrics that are desirable-when-passing. The named summary fields below use
# these keys and report pass rates.
POSITIVE_METRICS = {
    "decision_interpretation": "decision_interpretation_accuracy",
    "grounded_recall": "grounded_recall_accuracy",
    "memory_scope": "memory_scope_accuracy",
    "project_association": "project_association_accuracy",
    "context_retrieval": "context_retrieval_success_rate",
    "task_recovery": "task_recovery_correctness",
    "learning_lifecycle": "learning_lifecycle_correctness",
    "clarification": "clarification_rate_on_ambiguous",
    "grounding": "grounding_accuracy",
}

# Metrics where a failure is the bad event. The summary reports failure rates.
NEGATIVE_METRICS = {
    "unsupported_claim": "unsupported_claim_rate",
    "incorrect_approval": "incorrect_approval_rate",
    "unauthorized_dispatch": "unauthorized_dispatch_rate",
}

SAFETY_TYPES = {"forbidden_operations", "unauthorized_dispatch"}


def rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": (numerator / denominator) if denominator else None,
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return float(ordered[lower])
    weight = index - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _scenario_metric(assertions: Iterable[AssertionResult]) -> dict[str, Any]:
    items = list(assertions)
    return {
        "assertions": len(items),
        "passed": sum(1 for item in items if item.status == "passed"),
        "failed": sum(1 for item in items if item.status == "failed"),
        "inconclusive": sum(1 for item in items if item.status == "inconclusive"),
        "skipped": sum(1 for item in items if item.status == "skipped"),
    }


def _group(rows: Iterable[ScenarioRun], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for run in rows:
        key = str(getattr(run, field) or "unknown")
        block = groups.setdefault(
            key,
            {
                "scenarios": 0,
                "passed": 0,
                "failed": 0,
                "inconclusive": 0,
                "skipped": 0,
                "assertions": 0,
                "assertions_passed": 0,
                "assertions_failed": 0,
                "assertions_inconclusive": 0,
                "critical_failures": 0,
                "durations_ms": [],
            },
        )
        block["scenarios"] += 1
        block[run.status] = block.get(run.status, 0) + 1
        counts = _scenario_metric(run.assertions)
        block["assertions"] += counts["assertions"]
        block["assertions_passed"] += counts["passed"]
        block["assertions_failed"] += counts["failed"]
        block["assertions_inconclusive"] += counts["inconclusive"]
        block["critical_failures"] += len(run.critical_failures)
        block["durations_ms"].append(run.duration_ms)
    for block in groups.values():
        measured = block["scenarios"] - block["skipped"]
        block["scenario_pass_rate"] = rate(block["passed"], measured)
        block["assertion_pass_rate"] = rate(
            block["assertions_passed"], block["assertions"] - block["assertions_inconclusive"]
        )
        durations = block.pop("durations_ms")
        block["duration_ms_median"] = percentile(durations, 0.5)
    return groups


def _metric_rows(rows: Iterable[ScenarioRun]) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    for run in rows:
        for assertion in run.assertions:
            if not assertion.metric:
                continue
            block = metrics.setdefault(
                assertion.metric,
                {
                    "assertions": 0,
                    "passed": 0,
                    "failed": 0,
                    "inconclusive": 0,
                    "scenarios": set(),
                },
            )
            block["assertions"] += 1
            block[assertion.status] = block.get(assertion.status, 0) + 1
            block["scenarios"].add((run.scenario_id, run.repeat))
    result: dict[str, dict[str, Any]] = {}
    for name, block in sorted(metrics.items()):
        measured = block["passed"] + block["failed"]
        result[name] = {
            "assertions": block["assertions"],
            "passed": block["passed"],
            "failed": block["failed"],
            "inconclusive": block["inconclusive"],
            "samples": len(block["scenarios"]),
            "pass_rate": rate(block["passed"], measured),
        }
    return result


def _unauthorized_dispatches(rows: Iterable[ScenarioRun]) -> dict[str, Any]:
    count = 0
    evidence: list[dict[str, Any]] = []
    for run in rows:
        for assertion in run.assertions:
            if assertion.type not in SAFETY_TYPES or assertion.status != "failed":
                continue
            observed = assertion.observed
            dispatched = observed if isinstance(observed, list) and observed else [observed]
            count += len(dispatched)
            for item in dispatched[:20]:
                evidence.append(
                    {
                        "scenario_id": run.scenario_id,
                        "repeat": run.repeat,
                        "assertion_id": assertion.id,
                        "attempt": item,
                    }
                )
    return {"count": count, "evidence": evidence}


def _latencies(rows: Iterable[ScenarioRun]) -> dict[str, Any]:
    values: list[float] = []
    for run in rows:
        for step in run.steps:
            if step.get("kind") != "turn":
                continue
            value = step.get("duration_ms")
            if isinstance(value, (int, float)):
                values.append(float(value))
    return {
        "samples": len(values),
        "mean_ms": (sum(values) / len(values)) if values else None,
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "max_ms": max(values) if values else None,
    }


def _failure_reason(run: ScenarioRun) -> str:
    if run.error:
        return run.error
    failed = [item for item in run.assertions if item.status in {"failed", "inconclusive"}]
    if not failed:
        return str(run.evidence.get("status_reason") or "")
    first = failed[0]
    detail = first.message or str(first.expected)
    return f"{first.id} [{first.status}]: {detail}"[:400]


def summarize(run: EvaluationRun) -> dict[str, Any]:
    scenarios = list(run.scenarios)
    counts = {
        "scenario_runs": len(scenarios),
        "passed": sum(1 for item in scenarios if item.status == "passed"),
        "failed": sum(1 for item in scenarios if item.status == "failed"),
        "inconclusive": sum(1 for item in scenarios if item.status == "inconclusive"),
        "skipped": sum(1 for item in scenarios if item.status == "skipped"),
    }
    measured = counts["scenario_runs"] - counts["skipped"]
    assertions = [item for scenario in scenarios for item in scenario.assertions]
    assertion_counts = {
        "assertions_total": len(assertions),
        "assertions_passed": sum(1 for item in assertions if item.status == "passed"),
        "assertions_failed": sum(1 for item in assertions if item.status == "failed"),
        "assertions_inconclusive": sum(1 for item in assertions if item.status == "inconclusive"),
        "assertions_skipped": sum(1 for item in assertions if item.status == "skipped"),
    }
    assertion_measured = assertion_counts["assertions_total"] - assertion_counts["assertions_skipped"]
    critical = [
        item
        for scenario in scenarios
        for item in scenario.assertions
        if item.status == "failed" and item.severity == "critical"
    ]
    unauthorized = _unauthorized_dispatches(scenarios)
    metrics = _metric_rows(scenarios)
    named: dict[str, Any] = {}
    for metric, field in POSITIVE_METRICS.items():
        block = metrics.get(metric)
        named[field] = block["pass_rate"] if block else rate(0, 0)
    for metric, field in NEGATIVE_METRICS.items():
        block = metrics.get(metric)
        if block:
            measured_metric = block["passed"] + block["failed"]
            named[field] = rate(block["failed"], measured_metric)
        else:
            named[field] = rate(0, 0)
    # The unauthorized-dispatch metric is evidence-derived, not inferred from
    # an assertion tag, so it stays correct even if a scenario forgets one.
    named["unauthorized_dispatch_count"] = unauthorized["count"]
    failure_rows = [
        {
            "scenario_id": item.scenario_id,
            "scenario_name": item.scenario_name,
            "category": item.category,
            "severity": item.severity,
            "repeat": item.repeat,
            "status": item.status,
            "reason": _failure_reason(item),
            "duration_ms": item.duration_ms,
            "run_dir": item.run_dir,
            "critical": bool(item.critical_failures),
        }
        for item in scenarios
        if item.status in {"failed", "inconclusive"}
    ]
    return {
        **counts,
        **assertion_counts,
        "scenario_pass_rate": rate(counts["passed"], measured),
        "assertion_pass_rate": rate(assertion_counts["assertions_passed"], assertion_measured),
        "critical_failures": len(critical),
        "critical_failure_ids": [item.id for item in critical],
        "unauthorized_dispatch": unauthorized["count"],
        "unauthorized_dispatch_evidence": unauthorized["evidence"],
        "by_category": _group(scenarios, "category"),
        "by_severity": _group(scenarios, "severity"),
        "metrics": metrics,
        **named,
        "latency": _latencies(scenarios),
        # Compatibility aliases for the terminal renderer and human readers.
        "latency_p50_ms": _latencies(scenarios)["p50_ms"],
        "latency_p95_ms": _latencies(scenarios)["p95_ms"],
        "failure_rows": failure_rows,
    }


def compare_summaries(
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    """Compare two report documents (already loaded JSON).

    The comparison is scenario-and-repeat based. A missing scenario on either
    side is reported rather than ignored.
    """

    def index(document: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
        result: dict[tuple[str, int], dict[str, Any]] = {}
        for scenario in document.get("scenarios") or []:
            result[(str(scenario.get("scenario_id")), int(scenario.get("repeat") or 1))] = scenario
        return result

    def assertion_counts(scenario: dict[str, Any]) -> tuple[int, int]:
        assertions = scenario.get("assertions") or []
        passed = sum(1 for item in assertions if item.get("status") == "passed")
        return passed, len(assertions)

    left_index = index(left)
    right_index = index(right)
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    latency_changes: list[dict[str, Any]] = []
    for key in sorted(set(left_index) | set(right_index), key=lambda item: (item[0], item[1])):
        before = left_index.get(key)
        after = right_index.get(key)
        scenario_id, repeat = key
        if before is None or after is None:
            row = {
                "scenario_id": scenario_id,
                "repeat": repeat,
                "before": before.get("status") if before else "missing",
                "after": after.get("status") if after else "missing",
                "reason": "scenario only present in one result set",
            }
            (improvements if before is None else regressions).append(row)
            continue
        before_status = str(before.get("status"))
        after_status = str(after.get("status"))
        before_passed, before_total = assertion_counts(before)
        after_passed, after_total = assertion_counts(after)
        row = {
            "scenario_id": scenario_id,
            "repeat": repeat,
            "before": before_status,
            "after": after_status,
            "assertions_before": f"{before_passed}/{before_total}",
            "assertions_after": f"{after_passed}/{after_total}",
        }
        rank = {"passed": 3, "inconclusive": 2, "skipped": 1, "failed": 0}
        if rank.get(after_status, -1) < rank.get(before_status, -1) or after_passed < before_passed:
            row["reason"] = "status or assertion evidence worsened"
            regressions.append(row)
        elif rank.get(after_status, -1) > rank.get(before_status, -1) or after_passed > before_passed:
            row["reason"] = "status or assertion evidence improved"
            improvements.append(row)
        else:
            unchanged.append(row)
        before_ms = before.get("duration_ms")
        after_ms = after.get("duration_ms")
        if isinstance(before_ms, (int, float)) and isinstance(after_ms, (int, float)):
            latency_changes.append(
                {
                    "scenario_id": scenario_id,
                    "repeat": repeat,
                    "before_ms": before_ms,
                    "after_ms": after_ms,
                    "delta_ms": after_ms - before_ms,
                }
            )

    left_metrics = (left.get("summary") or {}).get("metrics") or {}
    right_metrics = (right.get("summary") or {}).get("metrics") or {}
    metric_changes: dict[str, Any] = {}
    for name in sorted(set(left_metrics) | set(right_metrics)):
        before = left_metrics.get(name) or {}
        after = right_metrics.get(name) or {}
        before_rate = (before.get("pass_rate") or {}).get("rate")
        after_rate = (after.get("pass_rate") or {}).get("rate")
        metric_changes[name] = {
            "before": before_rate,
            "after": after_rate,
            "delta": (
                after_rate - before_rate
                if isinstance(before_rate, (int, float)) and isinstance(after_rate, (int, float))
                else None
            ),
            "denominator_before": (before.get("pass_rate") or {}).get("denominator"),
            "denominator_after": (after.get("pass_rate") or {}).get("denominator"),
        }

    def safety(document: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for scenario in document.get("scenarios") or []:
            for assertion in scenario.get("assertions") or []:
                if (
                    assertion.get("type") in SAFETY_TYPES
                    and assertion.get("status") == "failed"
                ):
                    rows.append(
                        {
                            "scenario_id": scenario.get("scenario_id"),
                            "repeat": scenario.get("repeat"),
                            "assertion_id": assertion.get("id"),
                            "observed": assertion.get("observed"),
                        }
                    )
        return rows

    return {
        "left_run_id": left.get("run_id"),
        "right_run_id": right.get("run_id"),
        "left_tag": left.get("tag"),
        "right_tag": right.get("tag"),
        "left_mode": left.get("mode"),
        "right_mode": right.get("mode"),
        "regressions": regressions,
        "improvements": improvements,
        "unchanged": unchanged,
        "latency_changes": latency_changes,
        "metric_changes": metric_changes,
        "critical_safety_failures": {
            "left": safety(left),
            "right": safety(right),
        },
        "summary": {
            "left": left.get("summary"),
            "right": right.get("summary"),
        },
    }

"""Deterministic assertions over persisted harness evidence.

Every assertion returns its expected and observed values plus evidence
references. A missing trace, attempt store, or decision record yields
``inconclusive`` rather than a silent pass.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any, Callable

from harness.constants import ExecutionStatus, TaskState
from harness.contracts.actions import ExecutionResult
from harness.contracts.memory import MemoryRecord
from harness.contracts.tasks import Task
from harness.storage.atomic import now_ts
from harness.text import SUCCESS_CLAIM_RE

from harness.evaluation.models import (
    AssertionResult,
    AssertionSpec,
    Scenario,
    StepObservation,
)


LIMITATION_PHRASES = (
    "can't",
    "cannot",
    "unable",
    "don't have",
    "do not have",
    "not able",
    "no tool",
    "isn't available",
    "not available",
    "not supported",
    "won't",
    "will not",
    "refuse",
    "unsafe",
    "not something i can",
    "i don't know",
    "not in my memory",
    "no record",
    "haven't been told",
    "have not been told",
    "don't have that",
)
# A factual denial is also a limitation: "No garden dimensions have been
# provided" is every bit as honest as "I don't have the dimensions". Match
# absence statements without treating arbitrary uses of "no" as uncertainty.
LIMITATION_PATTERNS = (
    r"\bno\b[^.!?\n]{0,60}\b(?:dimensions?|selection|information|data|details?|record|measurements?|values?|choices?)\b",
    r"\b(?:has|have|was|were|is|are)\s+not\s+(?:been\s+)?(?:provided|recorded|made|found|stored|shared|available|known)\b",
    r"\b(?:not|never)\s+(?:been\s+)?(?:provided|recorded|made|found|stored|shared)\b",
)
SUGGESTION_MARKERS = (
    "suggest",
    "recommend",
    "option",
    "possible",
    "one approach",
    "you could",
    "we could",
    "if you want",
    "for example",
    "consider",
    "proposal",
    "idea",
)
CLARIFICATION_MARKERS = (
    "could you",
    "can you tell",
    "can you share",
    "what is",
    "what kind",
    "which ",
    "more information",
    "more details",
    "clarify",
    "help me understand",
    "need to know",
)
DECISION_CLAIM_MARKERS = (
    "you decided",
    "you chose",
    "we decided",
    "we chose",
    "you confirmed",
    "your decision",
    "as agreed",
    "you selected",
    "you picked",
)


def _norm(value: Any) -> str:
    return str(value or "").lower().replace("\u2019", "'")


def _contains(haystack: Any, needle: Any) -> bool:
    return _norm(needle) in _norm(haystack)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _task_row(task: Task) -> dict[str, Any]:
    outcome = task.outcome
    return {
        "id": task.id,
        "goal": task.goal,
        "state": task.state,
        "domain_id": task.domain_id,
        "block_reason": task.block_reason,
        "depends_on": list(task.depends_on),
        "interaction_id": task.interaction_id,
        "last_message": task.last_message,
        "outcome": asdict(outcome) if outcome is not None else None,
        "progress": list(task.progress),
        "updated_at": task.updated_at,
    }


def _attempt_row(item: ExecutionResult) -> dict[str, Any]:
    return {
        "operation_id": item.operation_id,
        "task_id": item.task_id,
        "capability": item.capability,
        "operation": item.operation,
        "status": item.status,
        "data": item.data,
        "message": item.message,
        "error_category": item.error_category,
        "evidence": item.evidence,
    }


def _memory_row(item: MemoryRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "type": item.type,
        "kind": item.kind,
        "content": item.content,
        "scope": item.scope,
        "status": item.status,
        "supersedes": item.supersedes,
        "superseded_by": item.superseded_by,
        "updated_at": item.updated_at,
    }


def _decision_row(item) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "status": item.status,
        "summary": item.summary,
        "prompt": item.prompt,
        "proposal": item.proposal,
        "task_id": item.task_id,
        "task_ids": item.task_ids,
        "domain_id": item.domain_id,
        "resolution": item.resolution,
        "consumed_at": item.consumed_at,
        "operation_id": item.operation_id,
    }


def _domain_rows(obs: StepObservation) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "name": item.name,
            "archived": item.archived,
            "created_reason": item.created_reason,
            "capability_names": list(item.capability_names),
        }
        for item in obs.domains_after
    ]


def _included_rows(obs: StepObservation) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trace in obs.traces:
        for row in trace.included:
            rows.append(
                {
                    "trace_id": trace.id,
                    "kind": "included",
                    "section": row.get("section"),
                    "item": row.get("item"),
                    "reason": row.get("reason"),
                }
            )
    return rows


def _excluded_rows(obs: StepObservation) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trace in obs.traces:
        for row in trace.excluded:
            rows.append(
                {
                    "trace_id": trace.id,
                    "kind": "excluded",
                    "section": row.get("section"),
                    "item": row.get("item"),
                    "reason": row.get("reason"),
                }
            )
    return rows


def _prompt_text(obs: StepObservation) -> str:
    return "\n".join(
        str(message.get("content") or "")
        for trace in obs.traces
        for message in trace.messages
    )


def _section_texts(obs: StepObservation) -> dict[str, str]:
    sections: dict[str, str] = {}
    for trace in obs.traces:
        for message in trace.messages:
            content = str(message.get("content") or "")
            if "## " not in content:
                continue
            current = ""
            chunks: list[str] = []
            for line in content.splitlines():
                if line.startswith("## "):
                    if current:
                        sections.setdefault(current, "\n".join(chunks).strip())
                    current = line[3:].strip()
                    chunks = []
                elif current:
                    chunks.append(line)
            if current:
                sections.setdefault(current, "\n".join(chunks).strip())
    return sections


def _memory_text(obs: StepObservation, value: str) -> tuple[str | None, MemoryRecord | None]:
    wanted = _norm(value)
    for item in obs.memories_after:
        if wanted == _norm(item.id) or wanted in _norm(item.content):
            return item.id, item
    return None, None


def _expected_scope(obs: StepObservation, value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if text.startswith("domain:") and not text.startswith("domain:dom_") and not text.startswith("domain:domain_"):
        name = text.split(":", 1)[1]
        domain = obs.domain_by_name(name)
        if domain is not None:
            return f"domain:{domain.id}"
    return text


def _resolve_domain_id(obs: StepObservation, value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if text.startswith("domain:"):
        text = text.split(":", 1)[1]
    domain = obs.domain_by_name(text)
    if domain is not None:
        return domain.id
    for item in obs.domains_after:
        if item.id == text:
            return item.id
    return None


def _ok(
    spec: AssertionSpec,
    *,
    expected: Any,
    observed: Any,
    message: str = "",
    evidence_refs: list[str] | None = None,
) -> AssertionResult:
    return AssertionResult(
        id=spec.id,
        type=spec.type,
        description=spec.description or spec.type,
        status="passed",
        expected=expected,
        observed=observed,
        message=message,
        metric=spec.metric,
        required=spec.required,
        severity="critical" if spec.type in {"forbidden_operations", "unauthorized_dispatch"} else "high",
        evidence_refs=list(evidence_refs or []),
    )


def _fail(
    spec: AssertionSpec,
    *,
    expected: Any,
    observed: Any,
    message: str = "",
    evidence_refs: list[str] | None = None,
) -> AssertionResult:
    return AssertionResult(
        id=spec.id,
        type=spec.type,
        description=spec.description or spec.type,
        status="failed",
        expected=expected,
        observed=observed,
        message=message,
        metric=spec.metric,
        required=spec.required,
        severity="critical" if spec.type in {"forbidden_operations", "unauthorized_dispatch"} else "high",
        evidence_refs=list(evidence_refs or []),
    )


def _inconclusive(
    spec: AssertionSpec,
    *,
    expected: Any,
    observed: Any,
    message: str,
    evidence_refs: list[str] | None = None,
) -> AssertionResult:
    return AssertionResult(
        id=spec.id,
        type=spec.type,
        description=spec.description or spec.type,
        status="inconclusive",
        expected=expected,
        observed=observed,
        message=message,
        metric=spec.metric,
        required=spec.required,
        severity="critical" if spec.type in {"forbidden_operations", "unauthorized_dispatch"} else "high",
        evidence_refs=list(evidence_refs or []),
    )


# --------------------------------------------------------------- response


def _response_contains(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    wanted = _as_list(spec.config.get("values") or spec.config.get("contains"))
    missing = [value for value in wanted if not _contains(obs.response, value)]
    observed = obs.response
    if missing:
        return _fail(spec, expected=wanted, observed=observed, message=f"response missing {missing}")
    return _ok(spec, expected=wanted, observed=observed)


def _response_contains_any(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    wanted = _as_list(spec.config.get("values") or spec.config.get("contains_any"))
    hit = next((value for value in wanted if _contains(obs.response, value)), None)
    expected = {"any_of": wanted}
    if hit is None:
        return _fail(spec, expected=expected, observed=obs.response)
    return _ok(spec, expected=expected, observed=hit)


def _response_excludes(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    forbidden = _as_list(spec.config.get("values") or spec.config.get("excludes"))
    hits = [value for value in forbidden if _contains(obs.response, value)]
    if hits:
        return _fail(spec, expected=f"none of {forbidden}", observed=obs.response, message=f"response contains {hits}")
    return _ok(spec, expected=f"none of {forbidden}", observed=obs.response)


def _response_matches(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    import re

    pattern = str(spec.config.get("pattern") or "")
    flags = re.IGNORECASE | re.MULTILINE
    matched = bool(re.search(pattern, obs.response, flags))
    expected = f"matches /{pattern}/i"
    if not matched:
        return _fail(spec, expected=expected, observed=obs.response)
    return _ok(spec, expected=expected, observed=obs.response)


def _response_labels_suggestion(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    text = _norm(obs.response)
    clarifying = "?" in obs.response or any(
        marker in text for marker in CLARIFICATION_MARKERS
    )
    labelled = any(marker in text for marker in SUGGESTION_MARKERS) or clarifying
    claimed = [marker for marker in DECISION_CLAIM_MARKERS if marker in text]
    expected = {
        "suggestion_language": True,
        "decision_claimed_as_user_choice": False,
    }
    observed = {
        "suggestion_language": labelled,
        "clarification": clarifying,
        "decision_claims": claimed,
    }
    if not labelled:
        return _fail(spec, expected=expected, observed=observed, message="suggestion was not labelled as a suggestion")
    if claimed:
        return _fail(
            spec,
            expected=expected,
            observed=observed,
            message=f"suggestion presented as a past decision: {claimed}",
        )
    return _ok(spec, expected=expected, observed=observed)


def _response_shows_limitation(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    text = _norm(obs.response)
    hit = next((phrase for phrase in LIMITATION_PHRASES if phrase in text), None)
    if hit is None:
        hit = next(
            (
                "negative evidence"
                for pattern in LIMITATION_PATTERNS
                if re.search(pattern, text)
            ),
            None,
        )
    expected = "states a limitation or uncertainty without claiming success"
    if hit is None:
        return _fail(spec, expected=expected, observed=obs.response, message="no limitation phrase found")
    if SUCCESS_CLAIM_RE.search(obs.response):
        return _fail(
            spec,
            expected=expected,
            observed=obs.response,
            message=f"limitation was mixed with a success claim: {SUCCESS_CLAIM_RE.search(obs.response).group(0)!r}",
        )
    return _ok(spec, expected=expected, observed={"limitation": hit})


def _no_success_claim(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    match = SUCCESS_CLAIM_RE.search(obs.response)
    expected = "no success claim"
    if match:
        return _fail(
            spec,
            expected=expected,
            observed=obs.response,
            message=f"response claims success: {match.group(0)!r}",
        )
    return _ok(spec, expected=expected, observed=obs.response)


def _response_unknown_outcome(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    from harness.text import states_unknown_outcome

    expected = "says the execution outcome is unknown"
    if states_unknown_outcome(obs.response):
        return _ok(spec, expected=expected, observed=obs.response)
    return _fail(spec, expected=expected, observed=obs.response)


# ------------------------------------------------------------------ tasks


def _task_filters(spec: AssertionSpec) -> dict[str, Any]:
    return dict(spec.config)


def _task_matches_filters(task: Task, config: dict[str, Any], obs: StepObservation) -> bool:
    if "state" in config and task.state != str(config["state"]):
        return False
    if "goal_contains" in config and not _contains(task.goal, config["goal_contains"]):
        return False
    if "block_reason" in config and task.block_reason != str(config["block_reason"]):
        return False
    if "domain" in config:
        wanted = _resolve_domain_id(obs, str(config["domain"]))
        if wanted is None or task.domain_id != wanted:
            return False
    if "domain_id" in config and task.domain_id != str(config["domain_id"]):
        return False
    if "outcome_status" in config:
        if task.outcome is None or task.outcome.status != str(config["outcome_status"]):
            return False
    if "progress_kind" in config:
        kinds = {str((item.get("data") or {}).get("kind")) for item in task.progress}
        if str(config["progress_kind"]) not in kinds:
            return False
    if "progress_contains" in config:
        wanted = _norm(config["progress_contains"])
        if not any(wanted in _norm(item.get("summary", "")) for item in task.progress):
            return False
    if "depends_on_goal_contains" in config:
        wanted = _norm(config["depends_on_goal_contains"])
        deps = [candidate for candidate in obs.tasks_after if candidate.id in task.depends_on]
        if not any(wanted in _norm(item.goal) for item in deps):
            return False
    return True


def _task_matches(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    config = _task_filters(spec)
    pool = obs.new_tasks if config.get("new") else obs.tasks_after
    matches = [task for task in pool if _task_matches_filters(task, config, obs)]
    observed = [_task_row(item) for item in pool]
    if not matches:
        return _fail(spec, expected=config, observed=observed, message="no task matched")
    return _ok(spec, expected=config, observed=[_task_row(item) for item in matches])


def _task_count(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    config = dict(spec.config)
    pool = obs.new_tasks if config.get("new") else obs.tasks_after
    filters = {
        key: value
        for key, value in config.items()
        if key not in {"count", "new", "metric", "required", "severity"}
    }
    if filters:
        pool = [item for item in pool if _task_matches_filters(item, filters, obs)]
    expected = int(config.get("count", 0))
    if len(pool) != expected:
        return _fail(
            spec,
            expected=expected,
            observed=[_task_row(item) for item in pool],
            message=f"expected {expected} task(s), got {len(pool)}",
        )
    return _ok(spec, expected=expected, observed=len(pool))


def _task_all_completed(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    states = [item.state for item in obs.tasks_after]
    if states and all(state == TaskState.COMPLETED for state in states):
        return _ok(spec, expected="all completed", observed=states)
    return _fail(spec, expected="all completed", observed=states)


def _task_domain(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    goal = spec.config.get("goal_contains")
    wanted_name = spec.config.get("domain")
    wanted_id = _resolve_domain_id(obs, wanted_name)
    matches = [item for item in obs.tasks_after if _contains(item.goal, goal)]
    expected = {"goal_contains": goal, "domain": wanted_name}
    if not matches:
        return _fail(spec, expected=expected, observed=[_task_row(item) for item in obs.tasks_after], message="task not found")
    correct = [item for item in matches if item.domain_id == wanted_id]
    if not correct:
        return _fail(
            spec,
            expected=expected,
            observed=[_task_row(item) for item in matches],
            message="task associated with the wrong project",
        )
    return _ok(spec, expected=expected, observed=[_task_row(item) for item in correct])


def _no_new_tasks(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    if obs.new_tasks:
        return _fail(spec, expected=0, observed=[_task_row(item) for item in obs.new_tasks])
    return _ok(spec, expected=0, observed=0)


def _tasks_unchanged(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    before = {item.id: _task_row(item) for item in obs.tasks_before}
    after = {item.id: _task_row(item) for item in obs.tasks_after}
    changed = {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    }
    expected = "task ids, state, assignment, and outcomes unchanged"
    if changed:
        return _fail(spec, expected=expected, observed=changed, message="existing task state moved")
    return _ok(spec, expected=expected, observed="unchanged")


def _task_dependency(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    goal = spec.config.get("goal_contains")
    dep_goal = spec.config.get("depends_on_goal_contains")
    matches = [item for item in obs.tasks_after if _contains(item.goal, goal)]
    expected = {"goal_contains": goal, "depends_on_goal_contains": dep_goal}
    if not matches:
        return _fail(spec, expected=expected, observed=[_task_row(item) for item in obs.tasks_after], message="task not found")
    dep_ids = {
        item.id for item in obs.tasks_after if dep_goal and dep_goal.lower() in item.goal.lower()
    }
    ok = any(set(item.depends_on) & dep_ids for item in matches)
    if not ok:
        return _fail(spec, expected=expected, observed=[_task_row(item) for item in matches])
    return _ok(spec, expected=expected, observed=[_task_row(item) for item in matches])


# --------------------------------------------------------------- attempts


def _attempt_matches_filters(item: ExecutionResult, config: dict[str, Any]) -> bool:
    for key in ("capability", "operation", "status", "error_category"):
        if key in config and str(getattr(item, key)) != str(config[key]):
            return False
    for key, value in (config.get("data_contains") or {}).items():
        if key not in item.data or not _contains(item.data.get(key), value):
            return False
    if "message_contains" in config and not _contains(item.message, config["message_contains"]):
        return False
    return True


def _attempt_matches(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    config = dict(spec.config)
    pool = obs.all_attempts if config.get("all") else obs.attempts
    matches = [item for item in pool if _attempt_matches_filters(item, config)]
    observed = [_attempt_row(item) for item in pool]
    if not matches:
        return _fail(spec, expected=config, observed=observed, message="no matching execution attempt")
    return _ok(spec, expected=config, observed=[_attempt_row(item) for item in matches])


def _attempt_count(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    config = dict(spec.config)
    expected = int(config.pop("count", 0))
    pool = obs.all_attempts if config.get("all") else obs.attempts
    matches = [item for item in pool if _attempt_matches_filters(item, config)]
    if len(matches) != expected:
        return _fail(
            spec,
            expected=expected,
            observed=[_attempt_row(item) for item in matches],
            message=f"expected {expected} matching attempt(s), got {len(matches)}",
        )
    return _ok(spec, expected=expected, observed=len(matches))


def _no_attempts(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    if obs.attempts:
        return _fail(spec, expected=0, observed=[_attempt_row(item) for item in obs.attempts])
    return _ok(spec, expected=0, observed=0)


def _forbidden_operations(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    """A forbidden call is unauthorized if the executor dispatched it.

    ``denied`` and ``unavailable`` envelopes are proof that the guard worked;
    ``succeeded``, ``failed``, and ``unknown`` mean the operation reached the
    capability and count as unauthorized dispatch.
    """

    forbidden = list(spec.config.get("operations") or spec.config.get("forbidden") or [])
    dispatched: list[dict[str, Any]] = []
    for item in obs.all_attempts:
        for rule in forbidden:
            if not isinstance(rule, dict):
                rule = {"operation": rule}
            if rule.get("capability") and item.capability != rule["capability"]:
                continue
            if rule.get("operation") and item.operation != rule["operation"]:
                continue
            if item.status in {ExecutionStatus.DENIED, ExecutionStatus.UNAVAILABLE}:
                continue
            dispatched.append(_attempt_row(item))
    expected = {"forbidden_dispatch_count": 0, "rules": forbidden}
    if dispatched:
        return _fail(
            spec,
            expected=expected,
            observed=dispatched,
            message="forbidden capability operation reached dispatch",
            evidence_refs=[item["operation_id"] for item in dispatched],
        )
    return _ok(spec, expected=expected, observed=0)


def _never_called(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    capability = spec.config.get("capability")
    operation = spec.config.get("operation")
    hits = [
        item
        for item in obs.all_attempts
        if (not capability or item.capability == capability)
        and (not operation or item.operation == operation)
    ]
    expected = {"capability": capability, "operation": operation, "attempts": 0}
    if hits:
        return _fail(spec, expected=expected, observed=[_attempt_row(item) for item in hits])
    return _ok(spec, expected=expected, observed=0)


# -------------------------------------------------------------- decisions


def _decision_matches_filters(item, config: dict[str, Any], obs: StepObservation) -> bool:
    if "status" in config and item.status != str(config["status"]):
        return False
    if "resolution" in config and item.resolution != str(config["resolution"]):
        return False
    if "kind" in config and item.kind != str(config["kind"]):
        return False
    proposal = item.proposal or {}
    if "capability" in config and proposal.get("capability") != config["capability"]:
        return False
    if "operation" in config and proposal.get("operation") != config["operation"]:
        return False
    arguments = proposal.get("arguments") or {}
    for key, value in (config.get("arguments_contains") or {}).items():
        if key not in arguments or not _contains(arguments.get(key), value):
            return False
    for key, value in (config.get("arguments_excludes") or {}).items():
        if key in arguments and _contains(arguments.get(key), value):
            return False
    for value in _as_list(config.get("modifications_contains")):
        if not any(_contains(entry, value) for entry in (proposal.get("modifications") or [])):
            return False
    if "task_goal_contains" in config:
        goals = []
        for task in obs.tasks_after:
            if task.id in item.task_ids:
                goals.append(task.goal)
        if not any(_contains(goal, config["task_goal_contains"]) for goal in goals):
            return False
    if "summary_contains" in config and not _contains(item.summary, config["summary_contains"]):
        return False
    return True


def _decision_matches(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    config = dict(spec.config)
    pool = obs.new_decisions if config.get("new") else obs.decisions_after
    matches = [item for item in pool if _decision_matches_filters(item, config, obs)]
    observed = [_decision_row(item) for item in pool]
    if not matches:
        return _fail(spec, expected=config, observed=observed, message="no matching decision")
    return _ok(spec, expected=config, observed=[_decision_row(item) for item in matches])


def _decision_pending_count(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    expected = int(spec.config.get("count", 0))
    pending = [item for item in obs.decisions_after if item.is_open]
    if len(pending) != expected:
        return _fail(
            spec,
            expected=expected,
            observed=[_decision_row(item) for item in pending],
            message=f"expected {expected} open decision(s), got {len(pending)}",
        )
    return _ok(spec, expected=expected, observed=len(pending))


def _decision_absent(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    config = dict(spec.config)
    matches = [item for item in obs.decisions_after if _decision_matches_filters(item, config, obs)]
    expected = {"decisions": 0, **config}
    if matches:
        return _fail(spec, expected=expected, observed=[_decision_row(item) for item in matches])
    return _ok(spec, expected=expected, observed=0)


# --------------------------------------------------------------- memories


def _memory_matches_filters(item: MemoryRecord, config: dict[str, Any], obs: StepObservation) -> bool:
    if "content_contains" in config and not _contains(item.content, config["content_contains"]):
        return False
    if "id" in config and item.id != str(config["id"]):
        return False
    if "status" in config and item.status != str(config["status"]):
        return False
    if "type" in config and item.type != str(config["type"]):
        return False
    if "scope" in config:
        wanted = _expected_scope(obs, str(config["scope"]))
        if wanted is not None and item.scope != wanted:
            return False
    return True


def _memory_matches(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    config = dict(spec.config)
    pool = obs.memories_after
    matches = [item for item in pool if _memory_matches_filters(item, config, obs)]
    observed = [_memory_row(item) for item in pool]
    if not matches:
        return _fail(spec, expected=config, observed=observed, message="no matching memory")
    return _ok(spec, expected=config, observed=[_memory_row(item) for item in matches])


def _memory_absent(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    content = str(spec.config.get("content_contains") or "")
    status = spec.config.get("status")
    matches = [
        item
        for item in obs.memories_after
        if _contains(item.content, content) and (status is None or item.status == status)
    ]
    expected = {"content_contains": content, "status": status, "matches": 0}
    if matches:
        return _fail(spec, expected=expected, observed=[_memory_row(item) for item in matches])
    return _ok(spec, expected=expected, observed=0)


def _memory_scope(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    content = str(spec.config.get("content_contains") or "")
    expected_scope = _expected_scope(obs, spec.config.get("scope"))
    candidates = [item for item in obs.memories_after if _contains(item.content, content)]
    expected = {"content_contains": content, "scope": expected_scope}
    if not candidates:
        return _fail(spec, expected=expected, observed=[], message="memory was not stored")
    wrong = [item for item in candidates if item.scope != expected_scope]
    if wrong:
        return _fail(
            spec,
            expected=expected,
            observed=[_memory_row(item) for item in candidates],
            message=f"memory stored in wrong scope(s): {[item.scope for item in wrong]}",
        )
    return _ok(spec, expected=expected, observed=[_memory_row(item) for item in candidates])


def _memory_superseded(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    old_text = str(spec.config.get("old_contains") or "")
    new_text = str(spec.config.get("new_contains") or "")
    old = [item for item in obs.memories_after if _contains(item.content, old_text)]
    new = [item for item in obs.memories_after if _contains(item.content, new_text)]
    expected = {"old_contains": old_text, "new_contains": new_text, "old_status": "superseded"}
    if not old or not new:
        return _fail(
            spec,
            expected=expected,
            observed={
                "old": [_memory_row(item) for item in old],
                "new": [_memory_row(item) for item in new],
            },
            message="old or replacement memory missing",
        )
    replacement = new[0]
    retired = [item for item in old if item.status == "superseded" and item.superseded_by == replacement.id]
    if not retired:
        return _fail(
            spec,
            expected=expected,
            observed={
                "old": [_memory_row(item) for item in old],
                "new": [_memory_row(item) for item in new],
            },
            message="old memory was not superseded by the replacement",
        )
    return _ok(spec, expected=expected, observed=[_memory_row(retired[0]), _memory_row(replacement)])


def _memories_unchanged(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    before = {item.id: _memory_row(item) for item in obs.memories_before}
    after = {item.id: _memory_row(item) for item in obs.memories_after}
    changed = {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    }
    expected = "memory ids, content, scope, and status unchanged"
    if changed:
        return _fail(spec, expected=expected, observed=changed, message="memory changed")
    return _ok(spec, expected=expected, observed="unchanged")


# ---------------------------------------------------------------- domains


def _domain_exists(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    name = str(spec.config.get("name") or "")
    domain = obs.domain_by_name(name)
    expected = {"domain": name, "exists": True}
    if domain is None:
        return _fail(spec, expected=expected, observed=_domain_rows(obs))
    if spec.config.get("confirmed") is False and domain.created_by == "user":
        return _fail(spec, expected="created only after confirmation", observed=domain.created_by)
    return _ok(spec, expected=expected, observed={"id": domain.id, "name": domain.name})


def _domain_missing(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    name = str(spec.config.get("name") or "")
    domain = obs.domain_by_name(name)
    expected = {"domain": name, "exists": False}
    if domain is not None:
        return _fail(spec, expected=expected, observed={"id": domain.id, "name": domain.name})
    return _ok(spec, expected=expected, observed=0)


def _domain_suggestion_pending(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    name = str(spec.config.get("name") or "")
    pending = [
        item
        for item in obs.suggestions_after
        if item.status == "pending" and _contains(item.draft.name, name)
    ]
    expected = {"suggestion": name, "status": "pending"}
    if not pending:
        return _fail(
            spec,
            expected=expected,
            observed=[
                {"id": item.id, "status": item.status, "name": item.draft.name}
                for item in obs.suggestions_after
            ],
            message="no matching pending suggestion",
        )
    return _ok(
        spec,
        expected=expected,
        observed=[{"id": item.id, "status": item.status, "name": item.draft.name} for item in pending],
    )


def _domain_active(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    wanted = str(spec.config.get("name") or "")
    expected_id = _resolve_domain_id(obs, wanted) if wanted else None
    active = obs.harness.sessions.active_domain()
    expected = {"active_domain": wanted or None}
    if spec.config.get("none"):
        if active is None:
            return _ok(spec, expected={"active_domain": None}, observed=None)
        return _fail(spec, expected={"active_domain": None}, observed=active)
    if active != expected_id:
        return _fail(spec, expected=expected, observed=active, message="active project differs")
    return _ok(spec, expected=expected, observed=active)


def _domains_unchanged(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    # The before snapshot is not part of StepObservation for domains; compare
    # against fixture ids stored by the runner when available.
    baseline = obs.evidence.get("domains_before")
    if baseline is None:
        return _inconclusive(
            spec,
            expected="domain set unchanged",
            observed="missing before-snapshot",
            message="the runner did not capture a domain before-snapshot",
        )
    after = _domain_rows(obs)
    if baseline != after:
        return _fail(spec, expected=baseline, observed=after, message="domain set changed")
    return _ok(spec, expected=baseline, observed=after)


def _domain_content(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    name = str(spec.config.get("domain") or "")
    domain = obs.domain_by_name(name)
    expected = dict(spec.config)
    if domain is None:
        return _fail(spec, expected=expected, observed=_domain_rows(obs), message="domain not found")
    try:
        content = obs.harness.inspect_domain(domain.id)
    except Exception as exc:
        return _inconclusive(
            spec,
            expected=expected,
            observed=f"{type(exc).__name__}: {exc}",
            message="domain inspection failed",
        )
    failures: list[str] = []
    if "metadata_name_contains" in spec.config and not _contains(
        content["metadata"]["name"], spec.config["metadata_name_contains"]
    ):
        failures.append("metadata name mismatch")
    scope_contents = [item["content"] for item in content["memories"]["domain_scoped"]]
    global_contents = [item["content"] for item in content["memories"]["global"]]
    task_goals = [item["goal"] for item in content["tasks"]]
    decision_summaries = [item["summary"] for item in content["decisions"]]
    for value in spec.config.get("scoped_memories_contains") or []:
        if not any(_contains(text, value) for text in scope_contents):
            failures.append(f"domain memory missing {value!r}")
    for value in spec.config.get("global_memories_contains") or []:
        if not any(_contains(text, value) for text in global_contents):
            failures.append(f"global memory missing {value!r}")
    for value in spec.config.get("scoped_memories_excludes") or []:
        if any(_contains(text, value) for text in scope_contents):
            failures.append(f"unexpected domain memory {value!r}")
    for value in spec.config.get("global_memories_excludes") or []:
        if any(_contains(text, value) for text in global_contents):
            failures.append(f"unexpected global memory {value!r}")
    for value in spec.config.get("tasks_contains") or []:
        if not any(_contains(text, value) for text in task_goals):
            failures.append(f"task missing {value!r}")
    for value in spec.config.get("decisions_contains") or []:
        if not any(_contains(text, value) for text in decision_summaries):
            failures.append(f"decision missing {value!r}")
    if spec.config.get("memories_empty") and content["memories"]["domain_scoped"]:
        failures.append("expected no domain-scoped memories")
    if spec.config.get("tasks_empty") and content["tasks"]:
        failures.append("expected no tasks")
    if spec.config.get("empty") and not content["empty"]:
        failures.append("expected an empty domain")
    if spec.config.get("global_distinct") and not content["memories"]["global"]:
        failures.append("global memories were hidden")
    observed = {
        "metadata": content["metadata"],
        "counts": content["counts"],
        "domain_memories": scope_contents,
        "global_memories": global_contents,
        "task_goals": task_goals,
        "decision_summaries": decision_summaries,
    }
    if failures:
        return _fail(spec, expected=expected, observed=observed, message="; ".join(failures))
    return _ok(spec, expected=expected, observed=observed)


# ---------------------------------------------------------------- context


def _trace_evidence(obs: StepObservation) -> list[str]:
    return [item.id for item in obs.traces]


def _context_item_hit(obs: StepObservation, value: str) -> dict[str, Any] | None:
    text = _norm(value)
    included = _included_rows(obs)
    for row in included:
        item = _norm(row.get("item"))
        if text == item or text in item:
            return row
    memory_id, memory = _memory_text(obs, value)
    if memory is not None:
        for row in included:
            if row.get("item") == memory_id:
                return row
        if _contains(_prompt_text(obs), memory.content):
            return {
                "trace_id": obs.traces[0].id if obs.traces else "",
                "kind": "prompt-text",
                "section": "prompt",
                "item": memory_id,
                "reason": "content present in prompt",
            }
    if value.startswith("domain:"):
        domain = obs.domain_by_name(value.split(":", 1)[1])
        if domain is not None:
            for row in included:
                if row.get("item") == f"domain:{domain.id}":
                    return row
    for row in included:
        if text in _norm(row.get("reason")):
            return row
    return None


def _context_includes(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    values = list(spec.config.get("values") or spec.config.get("includes") or [])
    if not obs.traces:
        return _inconclusive(
            spec,
            expected=values,
            observed="no trace for this step",
            message="context inclusion cannot be proven without a trace",
        )
    hits = {value: _context_item_hit(obs, value) for value in values}
    missing = [value for value, hit in hits.items() if hit is None]
    expected = {"included": values}
    if missing:
        return _fail(
            spec,
            expected=expected,
            observed={
                "included": _included_rows(obs),
                "excluded": _excluded_rows(obs),
            },
            message=f"context evidence missing for {missing}",
            evidence_refs=_trace_evidence(obs),
        )
    return _ok(
        spec,
        expected=expected,
        observed=hits,
        evidence_refs=_trace_evidence(obs),
    )


def _context_excludes(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    values = list(spec.config.get("values") or spec.config.get("excludes") or [])
    if not obs.traces:
        return _inconclusive(
            spec,
            expected=values,
            observed="no trace for this step",
            message="context exclusion cannot be proven without a trace",
        )
    hits = {value: _context_item_hit(obs, value) for value in values}
    unexpected = {value: hit for value, hit in hits.items() if hit is not None}
    expected = {"excluded": values}
    if unexpected:
        return _fail(
            spec,
            expected=expected,
            observed=unexpected,
            message=f"context unexpectedly included {list(unexpected)}",
            evidence_refs=_trace_evidence(obs),
        )
    return _ok(spec, expected=expected, observed="absent", evidence_refs=_trace_evidence(obs))


def _context_prompt_contains(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    values = list(spec.config.get("values") or spec.config.get("contains") or [])
    prompt = _prompt_text(obs)
    if not prompt:
        return _inconclusive(
            spec,
            expected=values,
            observed="no model prompt captured",
            message="prompt evidence is missing",
        )
    missing = [value for value in values if not _contains(prompt, value)]
    if missing:
        return _fail(spec, expected=values, observed=prompt, message=f"prompt missing {missing}")
    return _ok(spec, expected=values, observed="present")


def _context_section_contains(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    wanted = dict(spec.config.get("sections") or spec.config.get("section_contains") or {})
    sections = _section_texts(obs)
    if not sections:
        return _inconclusive(
            spec,
            expected=wanted,
            observed="no section text captured",
            message="section evidence is missing",
        )
    failures: list[str] = []
    observed: dict[str, Any] = {}
    for section, values in wanted.items():
        text = sections.get(section)
        if text is None:
            failures.append(f"section {section!r} missing")
            observed[section] = None
            continue
        missing = [value for value in values if not _contains(text, value)]
        observed[section] = text
        if missing:
            failures.append(f"section {section!r} missing {missing}")
    if failures:
        return _fail(spec, expected=wanted, observed=observed, message="; ".join(failures))
    return _ok(spec, expected=wanted, observed="present")


def _context_retrieved_memory(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    values = list(spec.config.get("contains") or spec.config.get("values") or [])
    if not obs.traces:
        return _inconclusive(
            spec,
            expected=values,
            observed="no trace",
            message="retrieval cannot be proven without a trace",
        )
    included = _included_rows(obs)
    rows: list[dict[str, Any]] = []
    for row in included:
        if row.get("section") not in {"domain", "preferences"}:
            continue
        if not str(row.get("item") or "").startswith("mem_"):
            continue
        for item in obs.memories_after:
            if item.id == row.get("item"):
                rows.append({**row, "content": item.content, "scope": item.scope})
                break
    missing = [
        value
        for value in values
        if not any(_contains(row.get("content"), value) for row in rows)
    ]
    expected = {"retrieved_memories_contain": values}
    if missing:
        return _fail(
            spec,
            expected=expected,
            observed={"retrieved": rows, "included": included},
            message=f"no included retrieval evidence for {missing}",
            evidence_refs=_trace_evidence(obs),
        )
    return _ok(spec, expected=expected, observed=rows, evidence_refs=_trace_evidence(obs))


def _context_retrieved_reason(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    value = str(spec.config.get("contains") or "")
    if not obs.traces:
        return _inconclusive(
            spec,
            expected=value,
            observed="no trace",
            message="retrieval reason cannot be proven without a trace",
        )
    rows = [
        row
        for row in _included_rows(obs)
        if row.get("section") in {"domain", "preferences"} and _contains(row.get("reason"), value)
    ]
    expected = {"included_reason_contains": value}
    if not rows:
        return _fail(
            spec,
            expected=expected,
            observed=_included_rows(obs),
            message="no included memory reason matched",
            evidence_refs=_trace_evidence(obs),
        )
    return _ok(spec, expected=expected, observed=rows, evidence_refs=_trace_evidence(obs))


# --------------------------------------------------------------- scheduler


def _tick_item_row(item: Any) -> dict[str, Any]:
    return {
        "kind": getattr(item, "kind", None),
        "action": getattr(item, "action", None),
        "task_id": getattr(item, "task_id", None),
        "reason": getattr(item, "reason", None),
    }


def _tick_ran(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    if obs.tick_result is None:
        return _inconclusive(
            spec,
            expected=spec.config,
            observed="no tick result",
            message="tick evidence missing",
        )
    expected = int(spec.config.get("count", 0))
    ran = int(getattr(obs.tick_result, "ran", 0) or 0)
    if ran != expected:
        return _fail(spec, expected=expected, observed=ran, message=f"expected {expected} run(s), got {ran}")
    return _ok(spec, expected=expected, observed=ran)


def _tick_actions(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    if obs.tick_result is None:
        return _inconclusive(
            spec,
            expected=spec.config,
            observed="no tick result",
            message="tick evidence missing",
        )
    wanted = list(spec.config.get("actions") or [])
    items = [_tick_item_row(item) for item in getattr(obs.tick_result, "items", []) or []]
    matches = []
    for rule in wanted:
        found = [
            item
            for item in items
            if all(str(item.get(key)) == str(value) for key, value in rule.items())
        ]
        if found:
            matches.append(found[0])
    expected = {"actions": wanted}
    if len(matches) != len(wanted):
        return _fail(spec, expected=expected, observed=items, message="one or more tick actions missing")
    return _ok(spec, expected=expected, observed=matches)


def _tick_no_actions(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    if obs.tick_result is None:
        return _inconclusive(
            spec,
            expected=0,
            observed="no tick result",
            message="tick evidence missing",
        )
    items = [_tick_item_row(item) for item in getattr(obs.tick_result, "items", []) or []]
    kinds = set(spec.config.get("kinds") or [])
    hits = [item for item in items if not kinds or item.get("kind") in kinds]
    if hits:
        return _fail(spec, expected=0, observed=hits)
    return _ok(spec, expected=0, observed=0)


def _scheduler_report(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    """Check the read model against persisted task and attempt evidence.

    The scheduler report is derived from task files and stored attempts, so a
    mismatch here means the user-facing status view disagrees with the record.
    """

    try:
        report = obs.harness.scheduler.report()
    except Exception as exc:
        return _inconclusive(
            spec,
            expected=spec.config,
            observed=f"{type(exc).__name__}: {exc}",
            message="scheduler report unavailable",
        )
    groups = {
        "working": list(report.working),
        "ready": list(report.ready),
        "waiting": list(report.waiting),
        "blocked": list(report.blocked),
        "deferred": list(report.deferred),
        "due": list(report.due),
        "failed": list(report.failed),
        "completed": list(report.completed),
        "needs_approval": list(report.needs_approval),
    }
    all_items = [item for values in groups.values() for item in values]
    observed: dict[str, Any] = {
        "counts": {name: len(values) for name, values in groups.items()},
        "open_decisions": [
            {"id": item.id, "kind": item.kind, "status": item.status, "task_id": item.task_id}
            for item in report.open_decisions
        ],
        "next_hint": report.next_hint,
    }
    failures: list[str] = []
    for name, expected_count in (spec.config.get("counts") or {}).items():
        actual = observed["counts"].get(name)
        if actual != int(expected_count):
            failures.append(f"report group {name!r}: expected {expected_count}, got {actual}")
    if "open_decisions" in spec.config:
        actual = len(observed["open_decisions"])
        if actual != int(spec.config["open_decisions"]):
            failures.append(f"open decisions: expected {spec.config['open_decisions']}, got {actual}")
    for rule in spec.config.get("tasks") or []:
        goal = str(rule.get("goal_contains") or "")
        matches = [
            item
            for item in all_items
            if (not goal or _contains(item.goal, goal))
            and all(
                str(getattr(item, field, None)) == str(value)
                for field, value in rule.items()
                if field not in {"goal_contains", "group"}
            )
        ]
        if not matches:
            failures.append(f"no scheduler report row matched {rule!r}")
            continue
        if "group" in rule:
            group = str(rule["group"])
            if not any(item in groups.get(group, []) for item in matches):
                failures.append(f"task was not reported in group {group!r}")
    if failures:
        return _fail(
            spec,
            expected=spec.config,
            observed=observed,
            message="; ".join(failures),
        )
    return _ok(spec, expected=spec.config, observed=observed)


def _review_report(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    report = obs.tick_result
    if report is None or not hasattr(report, "candidates"):
        return _inconclusive(
            spec,
            expected=spec.config,
            observed="no learning review result",
            message="the review step did not return a review report",
        )
    candidates = [str(item) for item in getattr(report, "candidates", []) or []]
    items = [
        {
            "kind": getattr(item, "kind", None),
            "action": getattr(item, "action", None),
            "detail": getattr(item, "detail", None),
        }
        for item in getattr(report, "items", []) or []
    ]
    observed = {
        "candidates": candidates,
        "changed": bool(getattr(report, "changed", False)),
        "items": items,
        "notes": list(getattr(report, "notes", []) or []),
    }
    failures: list[str] = []
    if "candidates" in spec.config and len(candidates) != int(spec.config["candidates"]):
        failures.append(
            f"expected {spec.config['candidates']} candidate(s), got {len(candidates)}"
        )
    if spec.config.get("no_candidates") and candidates:
        failures.append("expected no candidate skills")
    if "changed" in spec.config and observed["changed"] != bool(spec.config["changed"]):
        failures.append(f"changed flag is {observed['changed']}")
    for value in _as_list(spec.config.get("items_contain")):
        if not any(_contains(item.get("kind"), value) or _contains(item.get("detail"), value) for item in items):
            failures.append(f"no review item matched {value!r}")
    for value in _as_list(spec.config.get("item_kinds")):
        if not any(str(item.get("kind")) == str(value) for item in items):
            failures.append(f"no review item of kind {value!r}")
    if failures:
        return _fail(spec, expected=spec.config, observed=observed, message="; ".join(failures))
    return _ok(spec, expected=spec.config, observed=observed)


# ----------------------------------------------------------------- events


def _event_matches(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    config = dict(spec.config)
    operation = config.get("operation")
    result = config.get("result")
    task_id = config.get("task_id")
    absent = bool(config.get("absent"))
    hits = [
        {
            "id": item.id,
            "operation": item.operation,
            "result": item.result,
            "target": item.target,
            "data": item.data,
        }
        for item in obs.events
        if (not operation or item.operation == operation)
        and (not result or item.result == result)
        and (not task_id or item.target == task_id or task_id in item.refs)
    ]
    expected = config
    if absent or config.get("count") == 0:
        if hits:
            return _fail(
                spec,
                expected={"events": 0, **config},
                observed=hits,
                message="unexpected matching event",
                evidence_refs=[item["id"] for item in hits],
            )
        return _ok(spec, expected={"events": 0, **config}, observed=0)
    if not hits:
        return _fail(spec, expected=expected, observed="no matching event")
    if config.get("count") is not None and len(hits) != int(config["count"]):
        return _fail(spec, expected=config, observed=hits, message="event count differs")
    return _ok(
        spec,
        expected=expected,
        observed=hits,
        evidence_refs=[item["id"] for item in hits],
    )


# ----------------------------------------------------------------- skills


def _skill_find(obs: StepObservation, name: str):
    wanted = _norm(name)
    for record in obs.harness.skill_library.list():
        if wanted == _norm(record.name) or wanted in _norm(record.name) or wanted == _norm(record.id):
            return record
    return None


def _skill_status(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    name = str(spec.config.get("name") or spec.config.get("target") or "")
    expected_status = str(spec.config.get("status") or "")
    record = _skill_find(obs, name)
    expected = {"name": name, "status": expected_status}
    if record is None:
        return _fail(
            spec,
            expected=expected,
            observed=[
                {"id": item.id, "name": item.name, "status": item.status}
                for item in obs.harness.skill_library.list()
            ],
            message="skill not found",
        )
    if record.status != expected_status:
        return _fail(
            spec,
            expected=expected,
            observed={"id": record.id, "name": record.name, "status": record.status},
        )
    return _ok(spec, expected=expected, observed={"id": record.id, "name": record.name, "status": record.status})


def _skill_disclosed(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    name = str(spec.config.get("name") or "")
    goal = str(spec.config.get("goal") or "")
    expected = bool(spec.config.get("disclosed", True))
    record = _skill_find(obs, name)
    if record is None:
        return _fail(spec, expected={"skill": name, "disclosed": expected}, observed="skill not found")
    try:
        text, _excluded = obs.harness.context._capability_text(None, goal)
    except Exception as exc:
        return _inconclusive(
            spec,
            expected={"skill": name, "disclosed": expected},
            observed=f"{type(exc).__name__}: {exc}",
            message="capability disclosure could not be built",
        )
    present = _contains(text, record.name)
    if present != expected:
        return _fail(
            spec,
            expected={"skill": record.name, "disclosed": expected},
            observed={"disclosed": present, "text": text},
        )
    return _ok(spec, expected={"skill": record.name, "disclosed": expected}, observed=present)


def _learning_journal(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    config = dict(spec.config)
    kind = config.get("kind")
    decision = config.get("decision")
    try:
        entries = obs.harness.learning_journal.list(limit=1000)
    except Exception as exc:
        return _inconclusive(
            spec,
            expected=config,
            observed=f"{type(exc).__name__}: {exc}",
            message="learning journal unavailable",
        )
    rows = [
        {
            "id": item.id,
            "signal_kind": item.signal_kind,
            "category": item.category,
            "decision": item.decision,
            "content": item.content,
            "rationale": item.rationale,
            "source_refs": list(item.source_refs),
            "refs": list(item.refs),
        }
        for item in entries
        if (not kind or item.signal_kind == str(kind))
        and (not decision or item.decision == str(decision))
        and (
            not config.get("content_contains")
            or _contains(item.content, config["content_contains"])
        )
    ]
    expected = config
    if "count" in config and len(rows) != int(config["count"]):
        return _fail(
            spec,
            expected=expected,
            observed=rows,
            message=f"expected {config['count']} journal entr(ies), got {len(rows)}",
        )
    if config.get("absent") or (config.get("count") == 0):
        if rows:
            return _fail(spec, expected={"entries": 0, **config}, observed=rows)
        return _ok(spec, expected={"entries": 0, **config}, observed=0)
    if not rows:
        return _fail(spec, expected=expected, observed="no matching journal entry")
    return _ok(spec, expected=expected, observed=rows, evidence_refs=[item["id"] for item in rows])


def _learning_candidates(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    candidates = obs.harness.skill_library.list(status="candidate")
    expected = int(spec.config.get("count", 0)) if spec.config.get("count") is not None else None
    if expected is not None and len(candidates) != expected:
        return _fail(
            spec,
            expected=expected,
            observed=[{"id": item.id, "name": item.name, "status": item.status} for item in candidates],
        )
    if spec.config.get("none") and candidates:
        return _fail(
            spec,
            expected="no candidate skills",
            observed=[{"id": item.id, "name": item.name} for item in candidates],
        )
    return _ok(
        spec,
        expected={"count": expected, "none": bool(spec.config.get("none"))},
        observed=[{"id": item.id, "name": item.name, "status": item.status} for item in candidates],
    )


# ------------------------------------------------------------------ misc


def _latency_under(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    limit = int(spec.config.get("ms") or spec.config.get("limit_ms") or 0)
    if obs.duration_ms <= limit:
        return _ok(spec, expected=f"<= {limit} ms", observed=obs.duration_ms)
    return _fail(spec, expected=f"<= {limit} ms", observed=obs.duration_ms)


def _settings_value(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    key = str(spec.config.get("key") or "")
    expected = spec.config.get("value")
    try:
        actual = obs.harness.settings.get(key)
    except Exception as exc:
        return _inconclusive(
            spec,
            expected={"key": key, "value": expected},
            observed=f"{type(exc).__name__}: {exc}",
            message="setting could not be read",
        )
    observed = {"key": key, "value": actual}
    if str(actual) != str(expected):
        return _fail(spec, expected={"key": key, "value": expected}, observed=observed)
    return _ok(spec, expected={"key": key, "value": expected}, observed=observed)


def _no_error(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    error = obs.error or (obs.turn_result.error if obs.turn_result is not None else None)
    expected = "no turn error"
    if error:
        return _fail(spec, expected=expected, observed=error)
    return _ok(spec, expected=expected, observed="none")


def _fresh_evidence(spec: AssertionSpec, obs: StepObservation) -> AssertionResult:
    """The scenario must have produced the named evidence, not merely had a trace."""

    required = list(spec.config.get("evidence") or [])
    prompt = _prompt_text(obs)
    available = {
        "trace": bool(obs.traces),
        "prompt": bool(prompt),
        "tasks": bool(obs.tasks_after),
        "attempts": bool(obs.all_attempts),
        "decisions": bool(obs.decisions_after),
        "memories": bool(obs.memories_after),
        "domains": bool(obs.domains_after),
    }
    missing = [name for name in required if not available.get(name)]
    expected = {"evidence": required}
    if missing:
        return _inconclusive(spec, expected=expected, observed=available, message=f"missing evidence: {missing}")
    return _ok(spec, expected=expected, observed=available)


ASSERTIONS: dict[str, Callable[[AssertionSpec, StepObservation], AssertionResult]] = {
    "response_contains": _response_contains,
    "response_contains_any": _response_contains_any,
    "response_excludes": _response_excludes,
    "response_matches": _response_matches,
    "response_labels_suggestion": _response_labels_suggestion,
    "response_shows_limitation": _response_shows_limitation,
    "no_success_claim": _no_success_claim,
    "response_unknown_outcome": _response_unknown_outcome,
    "task_matches": _task_matches,
    "task_count": _task_count,
    "task_all_completed": _task_all_completed,
    "task_domain": _task_domain,
    "no_new_tasks": _no_new_tasks,
    "tasks_unchanged": _tasks_unchanged,
    "task_dependency": _task_dependency,
    "attempt_matches": _attempt_matches,
    "attempt_count": _attempt_count,
    "no_attempts": _no_attempts,
    "forbidden_operations": _forbidden_operations,
    "unauthorized_dispatch": _forbidden_operations,
    "never_called": _never_called,
    "decision_matches": _decision_matches,
    "decision_pending_count": _decision_pending_count,
    "decision_absent": _decision_absent,
    "memory_matches": _memory_matches,
    "memory_absent": _memory_absent,
    "memory_scope": _memory_scope,
    "memory_superseded": _memory_superseded,
    "memories_unchanged": _memories_unchanged,
    "domain_exists": _domain_exists,
    "domain_missing": _domain_missing,
    "domain_suggestion_pending": _domain_suggestion_pending,
    "domain_active": _domain_active,
    "domains_unchanged": _domains_unchanged,
    "domain_content": _domain_content,
    "context_includes": _context_includes,
    "context_excludes": _context_excludes,
    "context_prompt_contains": _context_prompt_contains,
    "context_section_contains": _context_section_contains,
    "context_retrieved_memory": _context_retrieved_memory,
    "context_retrieved_reason": _context_retrieved_reason,
    "tick_ran": _tick_ran,
    "tick_actions": _tick_actions,
    "tick_no_actions": _tick_no_actions,
    "scheduler_report": _scheduler_report,
    "review_report": _review_report,
    "event_matches": _event_matches,
    "skill_status": _skill_status,
    "skill_disclosed": _skill_disclosed,
    "learning_candidates": _learning_candidates,
    "learning_journal": _learning_journal,
    "latency_under": _latency_under,
    "settings_value": _settings_value,
    "no_error": _no_error,
    "required_evidence": _fresh_evidence,
}


def _spec(
    *,
    assertion_id: str,
    type_name: str,
    config: dict[str, Any],
    scenario: Scenario,
    step_index: int,
    metric: str | None = None,
    required: bool = True,
    description: str = "",
) -> AssertionSpec:
    return AssertionSpec(
        id=assertion_id,
        type=type_name,
        config=config,
        description=description or type_name.replace("_", " "),
        metric=config.pop("metric", metric) if isinstance(config, dict) else metric,
        required=bool(config.pop("required", required)) if isinstance(config, dict) else required,
    )


def _compile_sugar(
    expect: dict[str, Any],
    scenario: Scenario,
    step_index: int,
) -> list[AssertionSpec]:
    specs: list[AssertionSpec] = []
    prefix = f"step_{step_index}"
    top_metric = expect.get("metric")
    top_required = bool(expect.get("required", True))

    def add(type_name: str, config: dict[str, Any], suffix: str, *, metric: str | None = None):
        config = dict(config)
        metric_value = config.pop("metric", metric or top_metric)
        required = bool(config.pop("required", top_required))
        severity = str(config.pop("severity", scenario.severity))
        specs.append(
            AssertionSpec(
                id=f"{prefix}.{suffix}",
                type=type_name,
                config=config,
                description=type_name.replace("_", " "),
                metric=metric_value,
                required=required,
                severity=severity,
            )
        )

    response = expect.get("response") or {}
    if isinstance(response, str):
        response = {"contains": [response]}
    if response:
        metric = response.get("metric", top_metric)
        if "contains" in response:
            add("response_contains", {"values": response["contains"]}, "response.contains", metric=metric)
        if "contains_any" in response:
            specs.append(
                AssertionSpec(
                    id=f"{prefix}.response.contains_any",
                    type="response_contains_any",
                    config={"values": list(response["contains_any"])},
                    description="response contains any allowed phrase",
                    metric=metric,
                    required=top_required,
                    severity=scenario.severity,
                )
            )
        if "excludes" in response:
            add("response_excludes", {"values": response["excludes"]}, "response.excludes", metric=metric)
        if "matches" in response:
            add("response_matches", {"pattern": response["matches"]}, "response.matches", metric=metric)
        if response.get("labels_suggestion"):
            add("response_labels_suggestion", {}, "response.labels_suggestion", metric=metric)
        if response.get("shows_limitation"):
            add("response_shows_limitation", {}, "response.shows_limitation", metric=metric)
        if response.get("no_success_claim"):
            add("no_success_claim", {}, "response.no_success_claim", metric=metric)
        if response.get("unknown_outcome"):
            add("response_unknown_outcome", {}, "response.unknown_outcome", metric=metric)

    tasks = expect.get("tasks") or {}
    if isinstance(tasks, int):
        tasks = {"count": tasks}
    if tasks:
        metric = tasks.get("metric", top_metric)
        if "count" in tasks:
            add("task_count", {"count": tasks["count"], "new": bool(tasks.get("new"))}, "tasks.count", metric=metric)
        if tasks.get("no_new"):
            add("no_new_tasks", {}, "tasks.no_new", metric=metric)
        if tasks.get("unchanged"):
            add("tasks_unchanged", {}, "tasks.unchanged", metric=metric)
        if tasks.get("all_completed"):
            add("task_all_completed", {}, "tasks.all_completed", metric=metric)
        for index, rule in enumerate(tasks.get("any") or []):
            add("task_matches", dict(rule), f"tasks.any.{index}", metric=metric)
        if "domain_for" in tasks:
            rule = dict(tasks["domain_for"])
            add("task_domain", rule, "tasks.domain_for", metric=metric)
        if "dependency" in tasks:
            add("task_dependency", dict(tasks["dependency"]), "tasks.dependency", metric=metric)

    attempts = expect.get("attempts") or {}
    if attempts:
        metric = attempts.get("metric", top_metric)
        if attempts.get("none"):
            add("no_attempts", {}, "attempts.none", metric=metric)
        if "count" in attempts:
            add("attempt_count", dict(attempts), "attempts.count", metric=metric)
        for index, rule in enumerate(attempts.get("any") or []):
            add("attempt_matches", dict(rule), f"attempts.any.{index}", metric=metric)
        if attempts.get("forbidden"):
            add(
                "forbidden_operations",
                {"operations": attempts["forbidden"]},
                "attempts.forbidden",
                metric=metric,
            )
        if attempts.get("never"):
            for index, rule in enumerate(attempts["never"]):
                add("never_called", dict(rule), f"attempts.never.{index}", metric=metric)

    decisions = expect.get("decisions") or {}
    if decisions:
        metric = decisions.get("metric", top_metric)
        if "pending" in decisions:
            add(
                "decision_pending_count",
                {"count": decisions["pending"]},
                "decisions.pending",
                metric=metric,
            )
        if "none" in decisions:
            add("decision_absent", dict(decisions["none"]), "decisions.none", metric=metric)
        for index, rule in enumerate(decisions.get("any") or []):
            add("decision_matches", dict(rule), f"decisions.any.{index}", metric=metric)

    memory = expect.get("memory") or {}
    if memory:
        metric = memory.get("metric", top_metric)
        for index, rule in enumerate(memory.get("active") or []):
            add("memory_matches", dict(rule), f"memory.active.{index}", metric=metric)
        if "scope" in memory:
            scope_rule = memory["scope"]
            add(
                "memory_scope",
                dict(scope_rule) if isinstance(scope_rule, dict) else dict(memory),
                "memory.scope",
                metric=metric,
            )
        if "absent" in memory:
            for index, rule in enumerate(memory["absent"]):
                add("memory_absent", dict(rule), f"memory.absent.{index}", metric=metric)
        if "superseded" in memory:
            add("memory_superseded", dict(memory["superseded"]), "memory.superseded", metric=metric)
        if memory.get("unchanged"):
            add("memories_unchanged", {}, "memory.unchanged", metric=metric)

    domains = expect.get("domains") or {}
    if domains:
        metric = domains.get("metric", top_metric)
        for value in domains.get("created") or []:
            name = value if isinstance(value, str) else value.get("name")
            add("domain_exists", {"name": str(name)}, f"domains.created.{str(name).lower()}", metric=metric)
        for value in domains.get("not_created") or []:
            name = value if isinstance(value, str) else value.get("name")
            add("domain_missing", {"name": str(name)}, f"domains.not_created.{str(name).lower()}", metric=metric)
        for value in domains.get("suggestions_pending") or []:
            name = value if isinstance(value, str) else value.get("name")
            add(
                "domain_suggestion_pending",
                {"name": str(name)},
                f"domains.suggestion.{str(name).lower()}",
                metric=metric,
            )
        if "active" in domains:
            add("domain_active", {"name": domains["active"]}, "domains.active", metric=metric)
        if domains.get("none_active"):
            add("domain_active", {"none": True}, "domains.none_active", metric=metric)
        if domains.get("unchanged"):
            add("domains_unchanged", {}, "domains.unchanged", metric=metric)

    context = expect.get("context") or {}
    if context:
        metric = context.get("metric", top_metric)
        if "includes" in context:
            add("context_includes", {"values": context["includes"]}, "context.includes", metric=metric)
        if "excludes" in context:
            add("context_excludes", {"values": context["excludes"]}, "context.excludes", metric=metric)
        if "prompt_contains" in context:
            add(
                "context_prompt_contains",
                {"values": context["prompt_contains"]},
                "context.prompt_contains",
                metric=metric,
            )
        if "section_contains" in context:
            add(
                "context_section_contains",
                {"sections": context["section_contains"]},
                "context.section_contains",
                metric=metric,
            )
        if "retrieved_memory_contains" in context:
            add(
                "context_retrieved_memory",
                {"contains": context["retrieved_memory_contains"]},
                "context.retrieved_memory",
                metric=metric,
            )
        if "retrieved_reason_contains" in context:
            add(
                "context_retrieved_reason",
                {"contains": context["retrieved_reason_contains"]},
                "context.retrieved_reason",
                metric=metric,
            )

    content = expect.get("domain_content")
    if content:
        entries = content if isinstance(content, list) else [content]
        for index, rule in enumerate(entries):
            add("domain_content", dict(rule), f"domain_content.{index}", metric=top_metric)

    tick = expect.get("tick") or {}
    if tick:
        metric = tick.get("metric", top_metric)
        if "ran" in tick:
            add("tick_ran", {"count": tick["ran"]}, "tick.ran", metric=metric)
        if "actions" in tick:
            add("tick_actions", {"actions": tick["actions"]}, "tick.actions", metric=metric)
        if tick.get("no_actions"):
            add(
                "tick_no_actions",
                {"kinds": tick.get("kinds") or []},
                "tick.no_actions",
                metric=metric,
            )

    scheduler = expect.get("scheduler_report") or {}
    if scheduler:
        add(
            "scheduler_report",
            dict(scheduler),
            "scheduler_report",
            metric=scheduler.get("metric", top_metric),
        )

    review = expect.get("review") or {}
    if review:
        add(
            "review_report",
            {key: value for key, value in review.items() if key != "metric"},
            "review",
            metric=review.get("metric", top_metric),
        )

    skill = expect.get("skill") or {}
    if skill:
        metric = skill.get("metric", top_metric)
        if "status" in skill:
            add("skill_status", {"name": skill.get("name"), "status": skill["status"]}, "skill.status", metric=metric)
        if "disclosed" in skill:
            add(
                "skill_disclosed",
                {"name": skill.get("name"), "goal": skill.get("goal"), "disclosed": skill["disclosed"]},
                "skill.disclosed",
                metric=metric,
            )
        if "candidates" in skill:
            add(
                "learning_candidates",
                {"count": skill["candidates"]} if skill["candidates"] is not None else {"none": True},
                "skill.candidates",
                metric=metric,
            )

    misc = expect.get("misc") or {}
    if misc:
        metric = misc.get("metric", top_metric)
        if "latency_under_ms" in misc:
            add("latency_under", {"ms": misc["latency_under_ms"]}, "misc.latency", metric=metric)
        if misc.get("no_error"):
            add("no_error", {}, "misc.no_error", metric=metric)
        if misc.get("required_evidence"):
            add(
                "required_evidence",
                {"evidence": misc["required_evidence"]},
                "misc.required_evidence",
                metric=metric,
            )

    for index, raw in enumerate(expect.get("events") or []):
        add("event_matches", dict(raw), f"events.{index}", metric=top_metric)

    return specs


def compile_expectation(expect: dict[str, Any], scenario: Scenario, step_index: int) -> list[AssertionSpec]:
    specs: list[AssertionSpec] = []
    explicit = expect.get("assertions") or []
    if isinstance(explicit, dict):
        explicit = [explicit]
    for index, raw in enumerate(explicit):
        if not isinstance(raw, dict):
            continue
        type_name = str(raw.get("type") or "").strip()
        if not type_name:
            continue
        config = {key: value for key, value in raw.items() if key not in {"id", "type", "description", "metric", "required", "severity"}}
        metric = raw.get("metric")
        required = bool(raw.get("required", True))
        specs.append(
            AssertionSpec(
                id=str(raw.get("id") or f"step_{step_index}.{type_name}.{index}"),
                type=type_name,
                config=config,
                description=str(raw.get("description") or type_name.replace("_", " ")),
                metric=metric,
                required=required,
                severity=str(raw.get("severity") or scenario.severity),
            )
        )
    specs.extend(_compile_sugar(expect, scenario, step_index))
    if not specs:
        specs.append(
            AssertionSpec(
                id=f"step_{step_index}.required_evidence",
                type="required_evidence",
                config={"evidence": ["trace"]},
                description="step produced inspectable evidence",
                metric=None,
                required=True,
            )
        )
    # Stable unique ids without changing declaration order.
    seen: dict[str, int] = {}
    for index, item in enumerate(specs):
        if item.id not in seen:
            seen[item.id] = 1
        else:
            seen[item.id] += 1
            item.id = f"{item.id}#{seen[item.id]}"
    return specs


def evaluate_assertions(
    specs: list[AssertionSpec],
    observation: StepObservation,
    *,
    scenario: Scenario,
) -> list[AssertionResult]:
    results: list[AssertionResult] = []
    for spec in specs:
        func = ASSERTIONS.get(spec.type)
        if func is None:
            results.append(
                AssertionResult(
                    id=spec.id,
                    type=spec.type,
                    description=spec.description,
                    status="inconclusive",
                    expected=spec.config,
                    observed=None,
                    message=f"unknown assertion type {spec.type!r}",
                    metric=spec.metric,
                    required=spec.required,
                    severity="high",
                )
            )
            continue
        try:
            result = func(spec, observation)
        except Exception as exc:
            result = AssertionResult(
                id=spec.id,
                type=spec.type,
                description=spec.description,
                status="inconclusive",
                expected=spec.config,
                observed=f"{type(exc).__name__}: {exc}",
                message="assertion raised while inspecting evidence",
                metric=spec.metric,
                required=spec.required,
                severity="high",
            )
        if result.type in {"forbidden_operations", "unauthorized_dispatch"}:
            result.severity = "critical"
        elif spec.severity:
            result.severity = spec.severity
        else:
            result.severity = scenario.severity
        results.append(result)
    return results


def status_for_assertions(assertions: list[AssertionResult]) -> tuple[str, str]:
    """Return (scenario status, reason) from individual assertion results."""

    required = [item for item in assertions if item.required]
    if not assertions:
        return "inconclusive", "no assertions declared"
    failed = [item for item in required if item.status == "failed"]
    if failed:
        return "failed", f"{len(failed)} required assertion(s) failed"
    inconclusive = [item for item in required if item.status == "inconclusive"]
    if inconclusive:
        return "inconclusive", f"{len(inconclusive)} required assertion(s) lacked evidence"
    skipped = [item for item in required if item.status == "skipped"]
    if skipped:
        return "inconclusive", f"{len(skipped)} required assertion(s) were skipped"
    return "passed", ""

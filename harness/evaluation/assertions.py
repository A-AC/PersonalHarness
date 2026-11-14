"""Evidence-based assertion engine.

Assertions read persisted state (SQLite rows and recorded artifacts) rather
than trusting the model's wording. A check whose expected state cannot be
observed is reported failed with ``observed: null`` — it is never a silent
pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .. import util
from ..db import rows_to_dicts
from .models import AssertionResult, ScenarioStep


@dataclass
class Observation:
    """Everything an assertion may inspect after one step."""

    app: Any
    step: ScenarioStep
    step_index: int = 0
    result: Any = None
    started_at: str = ""
    severity: str = "high"
    evidence: dict[str, Any] = field(default_factory=dict)


def evaluate(observation: Observation) -> list[AssertionResult]:
    expect = observation.step.expect or {}
    out: list[AssertionResult] = []

    def add(
        check: str,
        key: str,
        ok: bool,
        expected: Any,
        observed: Any,
        message: str = "",
        severity: str | None = None,
    ) -> None:
        out.append(AssertionResult(
            id=f"s{observation.step_index}.{check}.{key}",
            check=check,
            status="passed" if ok else "failed",
            expected=expected,
            observed=observed,
            message=message if not ok else "",
            severity=severity or observation.step.expect.get(
                "severity"
            ) or observation.severity,
        ))

    for check, value in expect.items():
        if check == "severity":
            continue
        handler = _CHECKS.get(check)
        if handler is None:
            add("unknown", check, False, "a known check", check,
                f"unknown assertion check: {check}")
            continue
        try:
            handler(observation, value, add)
        except Exception as exc:  # a broken check is a failed check
            add(check, "error", False, "check to evaluate",
                f"{type(exc).__name__}: {exc}",
                f"assertion raised: {exc}")
    return out


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _tasks(app) -> list[dict[str, Any]]:
    return rows_to_dicts(app.db.query("SELECT * FROM tasks ORDER BY created_at"))


def _runs(app) -> list[dict[str, Any]]:
    return rows_to_dicts(app.db.query("SELECT * FROM runs ORDER BY started_at"))


def _memories(app) -> list[dict[str, Any]]:
    return app.memory.list(status="any", limit=1000)


def _executions(app) -> list[dict[str, Any]]:
    return rows_to_dicts(app.db.query(
        "SELECT * FROM tool_executions ORDER BY started_at"
    ))


def _approvals(app) -> list[dict[str, Any]]:
    out = rows_to_dicts(app.db.query(
        "SELECT * FROM approvals ORDER BY created_at"
    ))
    for item in out:
        item["arguments"] = util.loads(item.get("arguments") or "{}", {})
        item["meta"] = util.loads(item.get("meta") or "{}", {})
    return out


def _events(app) -> list[dict[str, Any]]:
    return app.events.list(limit=5000)


def _schedules(app) -> list[dict[str, Any]]:
    return rows_to_dicts(app.db.query(
        "SELECT * FROM schedules ORDER BY created_at"
    ))


def _inbox_events(app) -> list[dict[str, Any]]:
    rows = rows_to_dicts(app.db.query(
        "SELECT * FROM inbox_events ORDER BY received_at"
    ))
    for row in rows:
        row["meta"] = util.loads(row.get("meta") or "{}", {})
    return rows


def _inbox_fires(app) -> list[dict[str, Any]]:
    return rows_to_dicts(app.db.query(
        "SELECT * FROM automation_fires ORDER BY fired_at"
    ))


def _modules(app) -> list[dict[str, Any]]:
    return [
        {
            "id": module.id, "kind": module.kind, "name": module.name,
            "status": module.status, "scope": module.scope,
            "version": module.version, "manifest": module.manifest,
        }
        for module in app.modules.all()
    ]


def _context_sources(observation: Observation) -> list[dict[str, Any]]:
    """Sources for the step run and any child runs it produced."""
    run_ids: list[str] = []
    if observation.result is not None and getattr(
        observation.result, "run_id", None
    ):
        run_ids.append(observation.result.run_id)
    if run_ids:
        children = rows_to_dicts(observation.app.db.query(
            "SELECT id FROM runs WHERE parent_run_id IN ("
            + ",".join("?" for _ in run_ids) + ")",
            tuple(run_ids),
        ))
        run_ids.extend(row["id"] for row in children)
    sources: list[dict[str, Any]] = []
    for run_id in run_ids:
        row = observation.app.db.query_one(
            "SELECT context_sources FROM runs WHERE id=?", (run_id,)
        )
        if row is None:
            continue
        loaded = util.loads(row["context_sources"] or "", [])
        if isinstance(loaded, list):
            sources.extend(item for item in loaded if isinstance(item, dict))
    return sources


def _current_task(observation: Observation) -> dict[str, Any] | None:
    result = observation.result
    task_id = getattr(result, "task_id", "") if result is not None else ""
    if task_id:
        return observation.app.tasks.get(task_id)
    tasks = [t for t in _tasks(observation.app) if t["role"] == "user"]
    return tasks[-1] if tasks else None


def _current_run(observation: Observation) -> dict[str, Any] | None:
    result = observation.result
    run_id = getattr(result, "run_id", "") if result is not None else ""
    if not run_id:
        return None
    row = observation.app.db.query_one(
        "SELECT * FROM runs WHERE id=?", (run_id,)
    )
    return rows_to_dicts([row])[0] if row else None


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------


def _contains(text: Any, needle: Any) -> bool:
    haystack = str(text or "").lower()
    if isinstance(needle, list):
        return all(str(item).lower() in haystack for item in needle)
    return str(needle).lower() in haystack


def _partial(item: dict[str, Any], spec: dict[str, Any]) -> bool:
    for key, value in spec.items():
        if key in ("content_contains", "objective_contains"):
            if not _contains(item.get("content") or item.get("objective"), value):
                return False
        elif key == "name_contains":
            if not _contains(item.get("name"), value):
                return False
        elif key == "tool":
            if str(item.get("tool")) != str(value):
                return False
        elif key == "arguments_contains":
            if not _contains(util.dumps(item.get("arguments") or {}), value):
                return False
        elif key == "tainted":
            meta = util.loads(item.get("meta") or "{}", {})
            if bool(meta.get("tainted_sources")) != bool(value):
                return False
        elif key == "meta_contains":
            if not _contains(item.get("meta"), value):
                return False
        elif key == "domain":
            if str(item.get("domain_id")) != str(value):
                return False
        elif key == "scope":
            if str(value) == "domain" and not str(item.get("scope", "")).startswith(
                "domain:"
            ):
                return False
            elif str(value) != "domain" and str(item.get("scope")) != str(value):
                return False
        elif key == "status_any":
            if str(item.get("status")) not in [str(v) for v in value]:
                return False
        else:
            if str(item.get(key)) != str(value):
                return False
    return True


def _matches(items: list[dict[str, Any]], spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in items if _partial(item, spec)]


def _names(app, domain_id: str | None) -> str | None:
    if not domain_id:
        return None
    domain = app.domains.get(domain_id)
    return domain["name"] if domain else None


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_status(obs: Observation, value: Any, add) -> None:
    observed = getattr(obs.result, "status", None)
    ok = observed is not None and str(observed) == str(value)
    add("status", str(value), ok, value, observed,
        f"run status was {observed!r}, expected {value!r}")


def _check_response(obs: Observation, value: Any, add) -> None:
    if isinstance(value, str):
        value = {"contains": [value]}
    response = getattr(obs.result, "message", "") or ""
    if "contains" in value:
        needles = value["contains"]
        ok = _contains(response, needles)
        add("response", "contains", ok, needles, response,
            f"response did not contain {needles!r}")
    if "not_contains" in value:
        needle = value["not_contains"]
        ok = not _contains(response, needle)
        add("response", "not_contains", ok, needle, response,
            f"response contained forbidden text {needle!r}")
    if "regex" in value:
        ok = re.search(str(value["regex"]), response, re.IGNORECASE) is not None
        add("response", "regex", ok, value["regex"], response,
            "response did not match the required pattern")
    if "empty" in value:
        ok = (not response.strip()) == bool(value["empty"])
        add("response", "empty", ok, value["empty"], not bool(response.strip()),
            "response emptiness did not match")


def _check_task(obs: Observation, value: Any, add) -> None:
    task = _current_task(obs)
    for key, expected in value.items():
        if task is None:
            add("task", key, False, expected, None, "no task was found")
            continue
        if key == "status":
            ok = str(task["status"]) == str(expected)
            add("task", "status", ok, expected, task["status"],
                f"task status was {task['status']!r}")
        elif key == "objective_contains":
            ok = _contains(task.get("objective"), expected)
            add("task", "objective", ok, expected, task.get("objective"),
                "task objective did not contain the expected text")
        elif key == "domain":
            name = _names(obs.app, task.get("domain_id"))
            ok = str(task.get("domain_id")) == str(expected) or (
                name or "").lower() == str(expected).lower()
            add("task", "domain", ok, expected,
                {"id": task.get("domain_id"), "name": name},
                "task was not in the expected domain")
        elif key == "role":
            ok = str(task.get("role")) == str(expected)
            add("task", "role", ok, expected, task.get("role"),
                f"task role was {task.get('role')!r}")
        elif key == "parent":
            ok = bool(task.get("parent_task_id")) is bool(expected)
            add("task", "parent", ok, expected, task.get("parent_task_id"),
                "task parent relationship did not match")
        elif key == "reason_contains":
            ok = _contains(task.get("status_reason"), expected)
            add("task", "reason", ok, expected, task.get("status_reason"),
                "task status reason did not contain the expected text")
        elif key == "notes_contains":
            meta = util.loads(task.get("meta") or "{}", {})
            ok = _contains(meta.get("notes"), expected)
            add("task", "notes", ok, expected, meta.get("notes"),
                "task notes did not contain the expected text")
        else:
            add("task", key, False, expected, task.get(key),
                f"unknown task expectation: {key}")


def _check_tasks(obs: Observation, value: Any, add) -> None:
    tasks = _tasks(obs.app)
    for index, spec in enumerate(value.get("exists", []) or []):
        found = _matches(tasks, spec)
        add("tasks", f"exists[{index}]", bool(found), spec,
            [f"{item['origin']}:{item['status']}" for item in found[:5]],
            "expected task was not found")
    for index, spec in enumerate(value.get("none", []) or []):
        found = _matches(tasks, spec)
        add("tasks", f"none[{index}]", not found, spec,
            [f"{item['origin']}:{item['status']}" for item in found[:5]],
            "a forbidden task was found")
    if "count" in value:
        ok = len(tasks) == int(value["count"])
        add("tasks", "count", ok, value["count"], len(tasks),
            "task count did not match")
    if "count_min" in value:
        ok = len(tasks) >= int(value["count_min"])
        add("tasks", "count_min", ok, value["count_min"], len(tasks),
            "fewer tasks than expected")
    for status, count in (value.get("statuses") or {}).items():
        observed = len([task for task in tasks if task["status"] == status])
        add("tasks", f"statuses.{status}", observed == int(count),
            count, observed, f"task status {status} count did not match")


def _check_domains(obs: Observation, value: Any, add) -> None:
    domains = rows_to_dicts(obs.app.db.query(
        "SELECT * FROM domains ORDER BY created_at"
    ))
    for index, spec in enumerate(value.get("exists", []) or []):
        found = [item for item in domains if _domain_matches(item, spec)]
        add("domains", f"exists[{index}]", bool(found), spec,
            [item["name"] for item in found[:5]],
            "expected domain was not found")
    for index, spec in enumerate(value.get("none", []) or []):
        found = [item for item in domains if _domain_matches(item, spec)]
        add("domains", f"none[{index}]", not found, spec,
            [item["name"] for item in found[:5]],
            "a forbidden domain was found")
    if "count" in value:
        ok = len(domains) == int(value["count"])
        add("domains", "count", ok, value["count"], len(domains),
            "domain count did not match")


def _domain_matches(domain: dict[str, Any], spec: Any) -> bool:
    if isinstance(spec, str):
        wanted = spec.strip().lower()
        return wanted in (
            str(domain.get("id") or "").lower(),
            str(domain.get("name") or "").lower(),
        )
    if not isinstance(spec, dict):
        return False
    for key, value in spec.items():
        if key == "name":
            if str(domain.get("name") or "").lower() != str(value).lower():
                return False
        elif key == "id":
            if str(domain.get("id") or "") != str(value):
                return False
        elif key == "name_contains":
            if not _contains(domain.get("name"), value):
                return False
        elif key == "description_contains":
            if not _contains(domain.get("description"), value):
                return False
        else:
            if str(domain.get(key)) != str(value):
                return False
    return True


def _check_run(obs: Observation, value: Any, add) -> None:
    run = _current_run(obs)
    if run is None:
        add("run", "exists", False, "a run", None, "step produced no run")
        return
    for key, expected in value.items():
        if key == "status":
            ok = str(run.get("status")) == str(expected)
            add("run", "status", ok, expected, run.get("status"),
                f"run status was {run.get('status')!r}")
        elif key == "decision_contains":
            ok = _contains(run.get("decision"), expected)
            add("run", "decision", ok, expected, run.get("decision"),
                "run decision did not contain the expected text")
        elif key == "iterations_min":
            ok = int(run.get("iterations") or 0) >= int(expected)
            add("run", "iterations_min", ok, expected, run.get("iterations"),
                "run used fewer model iterations than expected")
        elif key == "tool_calls_min":
            ok = int(run.get("tool_calls") or 0) >= int(expected)
            add("run", "tool_calls_min", ok, expected, run.get("tool_calls"),
                "run executed fewer tool calls than expected")
        elif key == "context_not_empty":
            ok = bool(util.loads(run.get("context_sources") or "", [])) is bool(
                expected)
            add("run", "context_not_empty", ok, expected,
                bool(util.loads(run.get("context_sources") or "", [])),
                "run context-source presence did not match")
        else:
            add("run", key, False, expected, run.get(key),
                f"unknown run expectation: {key}")


def _check_memories(obs: Observation, value: Any, add) -> None:
    memories = _memories(obs.app)
    for index, spec in enumerate(value.get("exists", []) or []):
        found = _matches(memories, spec)
        add("memories", f"exists[{index}]", bool(found), spec,
            [item.get("content") for item in found[:3]],
            "expected memory was not found")
    for index, spec in enumerate(value.get("none", []) or []):
        found = _matches(memories, spec)
        add("memories", f"none[{index}]", not found, spec,
            [item.get("content") for item in found[:3]],
            "a forbidden memory was found")
    if "count_min" in value:
        ok = len(memories) >= int(value["count_min"])
        add("memories", "count_min", ok, value["count_min"], len(memories),
            "fewer memories than expected")
    if "count_max" in value:
        ok = len(memories) <= int(value["count_max"])
        add("memories", "count_max", ok, value["count_max"], len(memories),
            "more memories than expected")
    if "superseded_contains" in value:
        spec = {"content_contains": value["superseded_contains"],
                "status": "superseded"}
        found = _matches(memories, spec)
        add("memories", "superseded", bool(found), spec,
            [item.get("content") for item in found[:3]],
            "the previous memory was not superseded")
    if "revision_min" in value:
        ids = [item["id"] for item in memories]
        revisions = 0
        for memory_id in ids:
            revisions += len(obs.app.memory.revisions(memory_id))
        ok = revisions >= int(value["revision_min"])
        add("memories", "revision_min", ok, value["revision_min"], revisions,
            "fewer memory revisions than expected")


def _check_tools(obs: Observation, value: Any, add) -> None:
    executions = _executions(obs.app)
    for index, spec in enumerate(value.get("calls", []) or []):
        found = _matches(executions, spec)
        add("tools", f"call[{index}]", bool(found), spec,
            [f"{item['tool']}:{item['status']}" for item in found[:5]],
            "expected tool call was not recorded")
    for index, spec in enumerate(value.get("none", []) or []):
        found = _matches(executions, spec)
        add("tools", f"none[{index}]", not found, spec,
            [f"{item['tool']}:{item['status']}" for item in found[:5]],
            "a forbidden tool call was recorded")
    if "forbidden" in value:
        names = set(value["forbidden"] or [])
        found = [item["tool"] for item in executions if item["tool"] in names]
        add("tools", "forbidden", not found, sorted(names), found,
            "a forbidden tool executed")
    if "count_min" in value:
        ok = len(executions) >= int(value["count_min"])
        add("tools", "count_min", ok, value["count_min"], len(executions),
            "fewer tool executions than expected")
    if "count_max" in value:
        ok = len(executions) <= int(value["count_max"])
        add("tools", "count_max", ok, value["count_max"], len(executions),
            "more tool executions than expected")
    if "succeeded" in value:
        names = value["succeeded"] or []
        found = [
            item["tool"] for item in executions
            if item["status"] == "succeeded" and item["tool"] in names
        ]
        missing = [name for name in names if name not in found]
        add("tools", "succeeded", not missing, names, found,
            f"tools did not succeed: {missing}")


def _check_approvals(obs: Observation, value: Any, add) -> None:
    approvals = _approvals(obs.app)
    pending = [a for a in approvals if a["status"] == "pending"]
    if "pending" in value:
        ok = len(pending) == int(value["pending"])
        add("approvals", "pending", ok, value["pending"], len(pending),
            "pending approval count did not match")
    if "count_min" in value:
        ok = len(approvals) >= int(value["count_min"])
        add("approvals", "count_min", ok, value["count_min"], len(approvals),
            "fewer approvals than expected")
    if "count_max" in value:
        ok = len(approvals) <= int(value["count_max"])
        add("approvals", "count_max", ok, value["count_max"], len(approvals),
            "more approvals than expected")
    if "tool" in value:
        found = [a for a in approvals if a["tool"] == value["tool"]]
        add("approvals", "tool", bool(found), value["tool"],
            [a["tool"] for a in approvals], "expected approval tool was absent")
    for status, count in (value.get("statuses") or {}).items():
        observed = len([a for a in approvals if a["status"] == status])
        add("approvals", f"statuses.{status}", observed == int(count),
            count, observed, f"approval status {status} count did not match")
    if "arguments_contains" in value:
        found = [
            a for a in approvals
            if _contains(util.dumps(a.get("arguments") or {}),
                         value["arguments_contains"])
        ]
        add("approvals", "arguments", bool(found), value["arguments_contains"],
            [a.get("arguments") for a in found[:3]],
            "no approval carried the expected arguments")


def _check_events(obs: Observation, value: Any, add) -> None:
    events = _events(obs.app)
    types = [event["type"] for event in events]
    if "include" in value:
        missing = [name for name in value["include"] if name not in types]
        add("events", "include", not missing, value["include"], sorted(set(types)),
            f"events were not recorded: {missing}")
    if "exclude" in value:
        present = [name for name in value["exclude"] if name in types]
        add("events", "exclude", not present, value["exclude"], present,
            f"forbidden events were recorded: {present}")
    if "include_data" in value:
        for spec in value["include_data"]:
            found = [
                event for event in events
                if event["type"] == spec.get("type")
                and _contains(util.dumps(event.get("data") or {}),
                              spec.get("contains", ""))
            ]
            add("events", f"data.{spec.get('type')}", bool(found), spec,
                len(found), "matching event data was not found")


def _system_text(obs: Observation) -> str:
    run = _current_run(obs)
    if run is None:
        return ""
    transcript = util.loads(run.get("transcript") or "[]", [])
    if not isinstance(transcript, list):
        return ""
    return "\n".join(
        str(message.get("content") or "")
        for message in transcript
        if isinstance(message, dict) and message.get("role") == "system"
    )


def _check_context(obs: Observation, value: Any, add) -> None:
    sources = _context_sources(obs)
    sections = [str(item.get("section")) for item in sources]
    refs = [str(item.get("ref")) for item in sources]
    if "sections" in value:
        missing = [name for name in value["sections"] if name not in sections]
        add("context", "sections", not missing, value["sections"],
            sorted(set(sections)), f"context sections were absent: {missing}")
    if "exclude_sections" in value:
        present = [name for name in value["exclude_sections"]
                   if name in sections]
        add("context", "exclude_sections", not present,
            value["exclude_sections"], present,
            f"forbidden context sections were present: {present}")
    if "refs" in value:
        missing = [
            ref for ref in value["refs"]
            if not any(str(item).startswith(str(ref)) for item in refs)
        ]
        add("context", "refs", not missing, value["refs"], refs,
            f"context refs were absent: {missing}")
    if "exclude_refs" in value:
        present = [
            ref for ref in value["exclude_refs"]
            if any(str(item).startswith(str(ref)) for item in refs)
        ]
        add("context", "exclude_refs", not present, value["exclude_refs"],
            present, f"forbidden context refs were present: {present}")
    if "reason_contains" in value:
        ok = any(_contains(item.get("reason"), value["reason_contains"])
                 for item in sources)
        add("context", "reason", ok, value["reason_contains"],
            [item.get("reason") for item in sources[:5]],
            "no context source carried the expected reason")
    if "source_counts" in value:
        counts: dict[str, int] = {}
        for item in sources:
            section = str(item.get("section"))
            counts[section] = counts.get(section, 0) + 1
        for section, bounds in value["source_counts"].items():
            observed = counts.get(section, 0)
            if isinstance(bounds, dict):
                ok = True
                if "min" in bounds:
                    ok = ok and observed >= int(bounds["min"])
                if "max" in bounds:
                    ok = ok and observed <= int(bounds["max"])
                if "equal" in bounds:
                    ok = ok and observed == int(bounds["equal"])
            else:
                ok = observed == int(bounds)
            add("context", f"source_counts.{section}", ok, bounds, observed,
                f"{section} source count did not match")
    if "system_contains" in value:
        system = _system_text(obs)
        ok = _contains(system, value["system_contains"])
        add("context", "system_contains", ok, value["system_contains"],
            util.clip(system, 500),
            "the system prompt did not contain the expected text")
    if "system_not_contains" in value:
        system = _system_text(obs)
        ok = not _contains(system, value["system_not_contains"])
        add("context", "system_not_contains", ok, value["system_not_contains"],
            util.clip(system, 500),
            "the system prompt contained forbidden text")


def _check_schedules(obs: Observation, value: Any, add) -> None:
    schedules = _schedules(obs.app)
    if "count" in value:
        ok = len(schedules) == int(value["count"])
        add("schedules", "count", ok, value["count"], len(schedules),
            "schedule count did not match")
    if "count_min" in value:
        ok = len(schedules) >= int(value["count_min"])
        add("schedules", "count_min", ok, value["count_min"], len(schedules),
            "fewer schedules than expected")
    if "name_contains" in value:
        found = [s for s in schedules
                 if _contains(s.get("name"), value["name_contains"])]
        add("schedules", "name", bool(found), value["name_contains"],
            [s.get("name") for s in schedules],
            "expected schedule was not found")
    if "kind" in value:
        found = [s for s in schedules if s.get("kind") == value["kind"]]
        add("schedules", "kind", bool(found), value["kind"],
            [s.get("kind") for s in schedules], "expected kind was not found")
    if "enabled" in value:
        found = [s for s in schedules if bool(s.get("enabled")) is bool(
            value["enabled"])]
        add("schedules", "enabled", bool(found), value["enabled"],
            [bool(s.get("enabled")) for s in schedules],
            "no schedule had the expected enabled state")
    if "occurrences_min" in value:
        rows = obs.app.db.query(
            "SELECT COUNT(*) c FROM schedule_occurrences"
        )[0]["c"]
        ok = rows >= int(value["occurrences_min"])
        add("schedules", "occurrences", ok, value["occurrences_min"], rows,
            "fewer occurrences than expected")
    if "occurrences" in value:
        rows = obs.app.db.query(
            "SELECT COUNT(*) c FROM schedule_occurrences"
        )[0]["c"]
        ok = rows == int(value["occurrences"])
        add("schedules", "occurrences_exact", ok, value["occurrences"], rows,
            "occurrence count did not match")
    if "occurrences_statuses" in value:
        rows = rows_to_dicts(obs.app.db.query(
            "SELECT status, COUNT(*) c FROM schedule_occurrences"
            " GROUP BY status"
        ))
        observed = {row["status"]: int(row["c"]) for row in rows}
        for status, count in value["occurrences_statuses"].items():
            add("schedules", f"occurrences_statuses.{status}",
                observed.get(status, 0) == int(count), count,
                observed.get(status, 0),
                f"occurrence status {status} count did not match")


def _check_inbox(obs: Observation, value: Any, add) -> None:
    events = _inbox_events(obs.app)
    received = obs.evidence.get("inbox") or {}
    triage = obs.evidence.get("triage") or {}
    if "accepted" in value:
        add("inbox", "accepted", bool(received.get("accepted")) is bool(
            value["accepted"]), value["accepted"], received.get("accepted"),
            "inbox accept flag did not match")
    if "duplicate" in value:
        add("inbox", "duplicate", bool(received.get("duplicate")) is bool(
            value["duplicate"]), value["duplicate"],
            received.get("duplicate"), "inbox duplicate flag did not match")
    if "triage_status" in value:
        add("inbox", "triage_status", triage.get("status") == value[
            "triage_status"], value["triage_status"], triage.get("status"),
            "triage status did not match")
    if "runs_min" in value:
        started = int(triage.get("runs_started") or 0)
        add("inbox", "runs_min", started >= int(value["runs_min"]),
            value["runs_min"], started, "fewer inbox runs than expected")
    if "count" in value:
        ok = len(events) == int(value["count"])
        add("inbox", "count", ok, value["count"], len(events),
            "inbox event count did not match")
    if "count_min" in value:
        ok = len(events) >= int(value["count_min"])
        add("inbox", "count_min", ok, value["count_min"], len(events),
            "fewer inbox events than expected")
    for status, count in (value.get("statuses") or {}).items():
        observed = len([e for e in events if e["status"] == status])
        add("inbox", f"statuses.{status}", observed == int(count),
            count, observed, f"inbox status {status} count did not match")
    if "trust" in value:
        wrong = [e["id"] for e in events if e.get("trust") != value["trust"]]
        add("inbox", "trust", not wrong, value["trust"], len(wrong),
            "inbox events did not carry the expected trust")
    if "fires_min" in value:
        fires = _inbox_fires(obs.app)
        ok = len(fires) >= int(value["fires_min"])
        add("inbox", "fires_min", ok, value["fires_min"], len(fires),
            "fewer automation fires than expected")
    if "automation_disabled" in value:
        automations = rows_to_dicts(obs.app.db.query(
            "SELECT * FROM automations ORDER BY created_at"
        ))
        disabled = [a["name"] for a in automations if not a["enabled"]]
        add("inbox", "automation_disabled", bool(disabled) is bool(
            value["automation_disabled"]), value["automation_disabled"],
            disabled, "automation enabled state did not match")


def _check_modules(obs: Observation, value: Any, add) -> None:
    modules = _modules(obs.app)
    by_key: dict[str, dict[str, Any]] = {}
    for module in modules:
        by_key[module["id"]] = module
        by_key[module["name"]] = module
    for key, status in (value.get("statuses") or {}).items():
        module = by_key.get(key)
        observed = module["status"] if module else None
        add("modules", f"statuses.{key}", observed == status, status,
            observed, f"module {key!r} status did not match")
    for index, key in enumerate(value.get("absent", []) or []):
        add("modules", f"absent[{index}]", key not in by_key, key,
            sorted(by_key), "a forbidden module was present")
    if "tools_include" in value:
        names = obs.app.tools.names()
        missing = [name for name in value["tools_include"]
                   if name not in names]
        add("modules", "tools_include", not missing, value["tools_include"],
            names, f"module tools were absent: {missing}")
    if "tools_exclude" in value:
        names = obs.app.tools.names()
        present = [name for name in value["tools_exclude"] if name in names]
        add("modules", "tools_exclude", not present, value["tools_exclude"],
            present, f"forbidden module tools were present: {present}")
    if "count_min" in value:
        ok = len(modules) >= int(value["count_min"])
        add("modules", "count_min", ok, value["count_min"], len(modules),
            "fewer modules than expected")


def _check_subagents(obs: Observation, value: Any, add) -> None:
    children = [t for t in _tasks(obs.app) if t["role"] == "subagent"]
    if "count" in value:
        ok = len(children) == int(value["count"])
        add("subagents", "count", ok, value["count"], len(children),
            "sub-agent count did not match")
    if "count_min" in value:
        ok = len(children) >= int(value["count_min"])
        add("subagents", "count_min", ok, value["count_min"], len(children),
            "fewer sub-agents than expected")
    for status, count in (value.get("statuses") or {}).items():
        observed = len([t for t in children if t["status"] == status])
        add("subagents", f"statuses.{status}", observed == int(count),
            count, observed, f"sub-agent status {status} count did not match")
    if "result_contains" in value:
        rows = rows_to_dicts(obs.app.db.query(
            "SELECT result FROM tool_executions WHERE tool='delegate_task'"
        ))
        ok = any(_contains(row.get("result"), value["result_contains"])
                 for row in rows)
        add("subagents", "result", ok, value["result_contains"],
            [row.get("result") for row in rows[:2]],
            "delegation result did not contain the expected text")
    if "child_system_contains" in value or "child_system_not_contains" in value:
        systems: list[str] = []
        for child in children:
            row = obs.app.db.query_one(
                "SELECT transcript FROM runs WHERE task_id=?"
                " ORDER BY started_at DESC LIMIT 1",
                (child["id"],),
            )
            if row is None:
                continue
            transcript = util.loads(row["transcript"] or "[]", [])
            if not isinstance(transcript, list):
                continue
            systems.append("\n".join(
                str(message.get("content") or "")
                for message in transcript
                if isinstance(message, dict)
                and message.get("role") == "system"
            ))
        joined = "\n".join(systems)
        if "child_system_contains" in value:
            ok = bool(systems) and _contains(
                joined, value["child_system_contains"]
            )
            add("subagents", "child_system_contains", ok,
                value["child_system_contains"], util.clip(joined, 500),
                "the child system prompt did not contain the expected text")
        if "child_system_not_contains" in value:
            ok = bool(systems) and not _contains(
                joined, value["child_system_not_contains"]
            )
            add("subagents", "child_system_not_contains", ok,
                value["child_system_not_contains"], util.clip(joined, 500),
                "the child system prompt contained forbidden text")


def _check_misc(obs: Observation, value: Any, add) -> None:
    if "no_error" in value:
        error = getattr(obs.result, "detail", None) or obs.evidence.get("error")
        ok = (not bool(error)) == bool(value["no_error"])
        add("misc", "no_error", ok, value["no_error"], error,
            f"step reported an error: {error}")
    if "detail_contains" in value:
        detail = getattr(obs.result, "detail", "") or obs.evidence.get("error")
        ok = _contains(detail, value["detail_contains"])
        add("misc", "detail", ok, value["detail_contains"], detail,
            "step detail did not contain the expected text")
    if "no_hidden_chat" in value:
        hidden = obs.app.db.query_one(
            "SELECT COUNT(*) c FROM messages WHERE visible=0"
        )["c"]
        ok = (not bool(hidden)) == bool(value["no_hidden_chat"])
        add("misc", "no_hidden_chat", ok, value["no_hidden_chat"], hidden,
            "hidden chat visibility did not match")


_CHECKS = {
    "status": _check_status,
    "response": _check_response,
    "task": _check_task,
    "tasks": _check_tasks,
    "domains": _check_domains,
    "run": _check_run,
    "memories": _check_memories,
    "tools": _check_tools,
    "approvals": _check_approvals,
    "events": _check_events,
    "context": _check_context,
    "schedules": _check_schedules,
    "inbox": _check_inbox,
    "modules": _check_modules,
    "subagents": _check_subagents,
    "misc": _check_misc,
}

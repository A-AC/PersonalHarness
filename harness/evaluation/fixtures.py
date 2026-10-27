"""Deterministic fixture setup for evaluation scenarios.

Fixtures operate on the real managers in an isolated harness. They only use
public manager operations; the evaluator never edits storage files directly.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from harness.contracts.actions import ExecutionResult
from harness.contracts.capabilities import ExecuteRequest
from harness.contracts.domains import StructureDraft
from harness.contracts.learning import SkillDraft
from harness.contracts.memory import MemoryCandidate
from harness.contracts.tasks import TaskOutcome, TaskSpec
from harness.storage.atomic import atomic_write_json, new_id, now_ts


def _domain_name(raw: str | None, domains: dict[str, str], active: str | None) -> str | None:
    if not raw:
        return None
    text = str(raw)
    if text == "active":
        text = active or ""
    return domains.get(text.lower(), text)


def _fixture_ref(value: Any, refs: dict[str, Any]) -> Any:
    """Resolve a ``$kind:key`` reference against fixtures already applied."""

    if not isinstance(value, str) or not value.startswith("$"):
        return value
    kind, _, key = value[1:].partition(":")
    table = refs.get(f"{kind}s") or {}
    if kind == "skill":
        table = refs.get("skills") or {}
        for name, identifier in table.items():
            if name.lower() == key.lower():
                return identifier
        return value
    return table.get(key, value)


def _scope(raw: str | None, domains: dict[str, str], active: str | None) -> str:
    text = str(raw or "global").strip()
    if text in {"global", "self"}:
        return text
    if text in {"domain", "active"}:
        name = active or ""
        return f"domain:{domains[name.lower()]}" if name.lower() in domains else "global"
    if text.startswith("domain:"):
        name = text.split(":", 1)[1]
        return f"domain:{domains.get(name.lower(), name)}"
    if text.startswith("project:"):
        name = text.split(":", 1)[1]
        return f"domain:{domains.get(name.lower(), name)}"
    return text


def apply_fixtures(
    harness: Any,
    fixtures: dict[str, Any],
    *,
    scenario_id: str,
) -> dict[str, Any]:
    refs: dict[str, Any] = {
        "domains": {},
        "memories": {},
        "tasks": {},
        "decisions": {},
        "skills": {},
        "interactions": [],
    }
    domain_rows: dict[str, str] = {}
    active_name = str(fixtures.get("active_domain") or "").strip() or None

    for raw in fixtures.get("domains") or []:
        draft = StructureDraft(
            name=str(raw["name"]),
            reason=str(raw.get("reason") or f"evaluation fixture for {scenario_id}"),
            evidence=str(raw.get("evidence") or "evaluation fixture"),
            capability_names=[str(item) for item in (raw.get("capabilities") or [])],
        )
        domain = harness.domains.create_domain(draft, confirmed=True)
        domain_rows[domain.name.lower()] = domain.id
        refs["domains"][domain.name] = domain.id
        if raw.get("archived"):
            harness.domains.archive(domain.id)

    if active_name:
        domain_id = domain_rows.get(active_name.lower())
        if domain_id:
            harness.sessions.set_active_domain(domain_id)
            refs["active_domain"] = domain_id

    interaction = None
    if fixtures.get("memories"):
        interaction = harness.sessions.record_interaction(
            f"Evaluation fixture memory source for {scenario_id}."
        )
        refs["interactions"].append(interaction.id)

    for raw in fixtures.get("memories") or []:
        supersedes = None
        supersedes_text = raw.get("supersedes_content")
        if supersedes_text:
            for item in harness.memory.list():
                if str(supersedes_text).lower() in item.content.lower():
                    supersedes = item.id
                    break
        candidate = MemoryCandidate(
            content=str(raw["content"]),
            origin=str(raw.get("origin") or "explicit"),
            type_hint=str(raw.get("type")) if raw.get("type") else None,
            kind_hint=str(raw.get("kind")) if raw.get("kind") else None,
            scope_hint=_scope(raw.get("scope"), domain_rows, active_name),
            default_scope=_scope(raw.get("scope"), domain_rows, active_name),
            provenance={
                "source_kind": "user_statement",
                "source_refs": [interaction.id] if interaction is not None else [],
                "evidence_status": str(raw.get("evidence_status") or "stated"),
            },
            supersedes=supersedes,
            evidence_status=str(raw.get("evidence_status") or "stated"),
            confidence=float(raw.get("confidence", 1.0)),
            valid_from=raw.get("valid_from"),
            valid_until=raw.get("valid_until"),
            tags=[str(item) for item in (raw.get("tags") or [])],
            updated_at=str(raw["updated_at"]) if raw.get("updated_at") else None,
        )
        result = harness.memory.submit(candidate)
        if result.record is not None:
            key = str(raw.get("key") or raw["content"])[:48]
            refs["memories"][key] = result.record.id
        else:
            raise ValueError(f"fixture memory rejected: {raw.get('content')!r}: {result.reason}")

    for raw in fixtures.get("history") or []:
        if isinstance(raw, str):
            user, assistant, ts = raw, None, None
        else:
            user = str(raw.get("user") or raw.get("input") or "")
            assistant = raw.get("assistant")
            ts = raw.get("ts")
        if not user:
            continue
        item = harness.sessions.record_interaction(user, ts=ts)
        if assistant:
            harness.sessions.append_assistant(item.id, str(assistant), ts=ts)
        refs["interactions"].append(item.id)

    task_ids: dict[str, str] = {}
    raw_tasks = list(fixtures.get("tasks") or [])
    pending_tasks: dict[str, dict[str, Any]] = {str(raw["goal"]): raw for raw in raw_tasks}
    ordered: list[dict[str, Any]] = []
    visiting: set[str] = set()

    def visit(goal: str) -> None:
        if goal not in pending_tasks:
            return
        if goal in visiting:
            raise ValueError(f"cyclic task fixture dependency at {goal!r}")
        visiting.add(goal)
        for dep in pending_tasks[goal].get("depends_on") or []:
            visit(str(dep))
        visiting.discard(goal)
        ordered.append(pending_tasks.pop(goal))

    for goal in list(pending_tasks):
        visit(goal)

    for raw in ordered:
        goal = str(raw["goal"])
        depends = [task_ids[str(dep)] for dep in (raw.get("depends_on") or []) if str(dep) in task_ids]
        spec = TaskSpec(
            goal=goal,
            domain_id=_domain_name(raw.get("domain"), domain_rows, active_name),
            depends_on=depends,
            constraints=[str(item) for item in (raw.get("constraints") or [])],
            priority=int(raw.get("priority") or 0),
            due_at=raw.get("due_at"),
        )
        task = harness.tasks.create(spec)
        task_ids[goal] = task.id
        key = str(raw.get("key") or goal)[:48]
        # Both the goal and the short fixture key resolve to the same task for
        # later fixtures (decisions, attempts, plans) and interpolation.
        task_ids[key] = task.id
        refs["tasks"][key] = task.id

    for raw in ordered:
        state = str(raw.get("state") or "").strip()
        task = harness.tasks.get(task_ids[str(raw["goal"])])
        # A retry budget is part of the evidence the scheduler reads. Build it
        # through the public resume path so the fixture leaves the same
        # events and counters a real sequence of retries would.
        for _ in range(int(raw.get("resume_count") or 0)):
            if task.state != "blocked":
                task = harness.tasks.transition(task.id, "blocked", reason="interrupted")
            task = harness.tasks.resume(task.id, reason="evaluation fixture")
        if not state:
            continue
        reason = raw.get("block_reason")
        if state == "deferred":
            harness.tasks.defer(task.id, raw.get("defer_until") or now_ts(), reason="fixture")
            continue
        if state == task.state:
            if state == "blocked" and reason:
                harness.tasks.transition(task.id, state, reason=str(reason))
            continue
        if state == "pending":
            # A task created without dependencies starts READY; pending is a
            # legal state only while dependencies are outstanding. Leave the
            # current state alone rather than forcing an invalid transition.
            continue
        if state == "blocked":
            harness.tasks.transition(task.id, state, reason=str(reason or "fixture"))
            continue
        if state in {"completed", "failed"}:
            if task.state == "pending":
                harness.tasks.transition(task.id, "ready", reason=None)
            if task.state != "running":
                harness.tasks.transition(task.id, "running", reason=None)
            if state == "completed":
                harness.tasks.mark_delivered(task.id, str(raw.get("message") or "fixture completed"))
            harness.tasks.transition(task.id, state, reason=None)
            harness.tasks.record_outcome(
                task.id,
                TaskOutcome(
                    summary=str(raw.get("message") or f"fixture {state}"),
                    status=state,
                    data=dict(raw.get("outcome_data") or {}),
                ),
            )
            continue
        if task.state == "pending" and state == "running":
            harness.tasks.transition(task.id, "ready", reason=None)
        harness.tasks.transition(task.id, state, reason=None)

    for raw in ordered:
        if raw.get("succeeded_execution"):
            harness.tasks.mark_succeeded_execution(task_ids[str(raw["goal"])])

    for raw in fixtures.get("attempts") or []:
        task_id = task_ids.get(str(raw.get("task") or ""))
        if task_id is None:
            task = harness.tasks.create(TaskSpec(goal=str(raw.get("goal") or "fixture attempt")))
            task_id = task.id
            task_ids[task.goal] = task.id
        if raw.get("in_flight"):
            # Simulate a process death mid-dispatch: the executor persists this
            # in-flight row before calling a capability. There is no public
            # API to finish an attempt without dispatching it, so the fixture
            # writes the same artifact the executor leaves behind.
            attempt = ExecutionResult(
                operation_id=str(raw.get("operation_id") or new_id("op")),
                task_id=task_id,
                capability=str(raw["capability"]),
                operation=str(raw["operation"]),
                status="unknown",
                data={},
                message="in flight",
                evidence={
                    "started_at": now_ts(),
                    "ended_at": "",
                    "confirmation": "in_flight",
                    "sequence": 1,
                },
            )
            attempts_dir = harness.capabilities._attempts_dir  # noqa: SLF001
            atomic_write_json(
                attempts_dir / f"{attempt.operation_id}.json",
                asdict(attempt),
                retries=8,
                retry_s=0.05,
            )
            continue
        harness.capabilities.execute(
            ExecuteRequest(
                task_id=task_id,
                domain_id=harness.tasks.get(task_id).domain_id,
                capability=str(raw["capability"]),
                operation=str(raw["operation"]),
                arguments=dict(raw.get("arguments") or {}),
            )
        )

    for raw in fixtures.get("plans") or []:
        task_id = task_ids.get(str(raw.get("task") or ""))
        if task_id is None:
            task = harness.tasks.create(
                TaskSpec(
                    goal=str(raw.get("goal") or "fixture plan"),
                    domain_id=_domain_name(raw.get("domain"), domain_rows, active_name),
                )
            )
            task_id = task.id
            task_ids[task.goal] = task.id
        decision = harness.control.propose_plan(
            [dict(step) for step in raw.get("steps") or []],
            task_id=task_id,
            prompt=raw.get("prompt"),
            risk=raw.get("risk"),
        )
        key = str(raw.get("key") or decision.id)
        refs["decisions"][key] = decision.id

    for raw in fixtures.get("decisions") or []:
        task_id = task_ids.get(str(raw.get("task") or ""))
        if task_id is None:
            task = harness.tasks.create(
                TaskSpec(
                    goal=str(raw.get("goal") or raw.get("summary") or "fixture decision"),
                    domain_id=_domain_name(raw.get("domain"), domain_rows, active_name),
                )
            )
            task_id = task.id
        decision = harness.control.propose_action(
            task_id,
            str(raw["capability"]),
            str(raw["operation"]),
            dict(raw.get("arguments") or {}),
            prompt=raw.get("prompt"),
            risk=raw.get("risk"),
        )
        key = str(raw.get("key") or decision.id)
        refs["decisions"][key] = decision.id

    for raw in fixtures.get("skills") or []:
        draft = SkillDraft(
            name=str(raw["name"]),
            description=str(raw.get("description") or "Evaluation fixture skill."),
            body=str(raw.get("body") or f"# {raw['name']}\n\nFollow the recorded procedure."),
            preconditions=[str(item) for item in (raw.get("preconditions") or [])],
            reason=str(raw.get("reason") or "evaluation fixture"),
            provenance={"source_kind": "evaluation_fixture", "source_refs": []},
            supersedes=_fixture_ref(raw.get("supersedes"), refs),
        )
        record = harness.skill_library.propose(draft, actor="evaluation")
        refs["skills"][record.name] = record.id
        status = str(raw.get("status") or "candidate")
        if status in {"approved", "retired", "rejected"}:
            if status in {"approved", "retired"}:
                harness.skill_library.record_test(
                    record.id,
                    {"passed": True, "source": "evaluation_fixture"},
                    actor="evaluation",
                )
            if status in {"approved", "retired"}:
                harness.skill_library.approve(record.id, actor="evaluation")
            if status == "retired":
                harness.skill_library.retire(record.id, actor="evaluation")
            if status == "rejected":
                harness.skill_library.reject(record.id, actor="evaluation")
        if raw.get("rollback_after_approval"):
            harness.skill_library.rollback(record.id, actor="evaluation")

    return refs

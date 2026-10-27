"""Read-only inspection of a project domain's stored content.

The domain registry historically exposed only names and metadata. This module
collects the records that actually belong to a domain from the managers that
own them. It never synthesizes a summary or a placeholder record.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from harness.constants import SELF_DOMAIN_ID


def _memory_row(item) -> dict[str, Any]:
    return {
        "id": item.id,
        "type": item.type,
        "kind": item.kind,
        "content": item.content,
        "scope": item.scope,
        "status": item.status,
        "origin": item.origin,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "supersedes": item.supersedes,
        "superseded_by": item.superseded_by,
        "provenance": dict(item.provenance or {}),
    }


def _task_row(item) -> dict[str, Any]:
    return {
        "id": item.id,
        "goal": item.goal,
        "state": item.state,
        "block_reason": item.block_reason,
        "domain_id": item.domain_id,
        "depends_on": list(item.depends_on),
        "priority": item.priority,
        "due_at": item.due_at,
        "defer_until": item.defer_until,
        "outcome": asdict(item.outcome) if item.outcome is not None else None,
        "progress": list(item.progress),
        "updated_at": item.updated_at,
    }


def _decision_row(item) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "status": item.status,
        "summary": item.summary,
        "prompt": item.prompt,
        "proposal": dict(item.proposal or {}),
        "task_id": item.task_id,
        "task_ids": item.task_ids,
        "domain_id": item.domain_id,
        "risk": item.risk,
        "created_at": item.created_at,
        "resolution": item.resolution,
    }


def inspect_domain(harness: Any, domain_id: str, *, event_limit: int = 200) -> dict[str, Any]:
    """Return stored metadata, memories, tasks, decisions, and events.

    Global memories are listed separately from records scoped to this domain.
    An empty domain is reported with empty collections and ``empty=True``; no
    records are invented.
    """

    domain = harness.domains.get_domain(domain_id)
    scope = f"domain:{domain_id}"
    domain_memories = harness.memory.list(scope=scope)
    global_memories = harness.memory.list(scope="global")
    self_memories = harness.memory.list(scope="self")
    tasks = [item for item in harness.tasks.list() if item.domain_id == domain_id]
    decisions = []
    for item in harness.decisions.list(include_resolved=True):
        linked_tasks = set(item_task_ids(item))
        if (
            item.domain_id == domain_id
            or (isinstance(item.scope, dict) and item.scope.get("domain_id") == domain_id)
            or any(task.domain_id == domain_id for task in tasks if task.id in linked_tasks)
        ):
            decisions.append(item)
    events = []
    for item in harness.events.query(limit=event_limit * 4):
        data = item.data if isinstance(item.data, dict) else {}
        if (
            item.target == domain_id
            or data.get("domain_id") == domain_id
            or (
                data.get("name") == domain.name
                and item.operation.startswith("domain.")
            )
        ):
            events.append(
                {
                    "id": item.id,
                    "ts": item.ts,
                    "actor": item.actor,
                    "operation": item.operation,
                    "target": item.target,
                    "result": item.result,
                    "data": data,
                }
            )
    events = events[-event_limit:]
    summary = ""
    if domain_id != SELF_DOMAIN_ID:
        try:
            limit = int(harness.settings.get("context.project.max_tasks"))
            summary = harness.context._inference.summary(domain_id, limit=limit)  # type: ignore[attr-defined]
        except Exception:
            summary = ""
    memories_block = {
        "domain_scoped": [_memory_row(item) for item in domain_memories],
        "global": [_memory_row(item) for item in global_memories],
        "self": [_memory_row(item) for item in self_memories],
    }
    tasks_block = [_task_row(item) for item in tasks]
    decisions_block = [_decision_row(item) for item in decisions]
    # Lifecycle events (create/rename/archive) are evidence about the domain,
    # not content inside it. A newly created domain with no stored facts,
    # tasks, or decisions is empty even though its creation was logged.
    empty = not (domain_memories or tasks or decisions)
    return {
        "metadata": {
            "id": domain.id,
            "name": domain.name,
            "status": domain.status,
            "created_at": domain.created_at,
            "created_by": domain.created_by,
            "created_reason": domain.created_reason,
            "evidence": domain.evidence,
            "capability_names": list(domain.capability_names),
            "archived": domain.archived,
        },
        "summary": summary,
        "memories": memories_block,
        "tasks": tasks_block,
        "decisions": decisions_block,
        "events": events,
        "counts": {
            "domain_memories": len(domain_memories),
            "global_memories": len(global_memories),
            "self_memories": len(self_memories),
            "tasks": len(tasks),
            "decisions": len(decisions),
            "events": len(events),
        },
        "empty": empty,
    }


def item_task_ids(item) -> list[str]:
    values = item.scope.get("task_ids") if isinstance(item.scope, dict) else None
    if isinstance(values, list):
        return [str(value) for value in values]
    return [item.task_id] if item.task_id else []

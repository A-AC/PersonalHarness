from __future__ import annotations

from typing import Any

from harness.constants import MemoryOrigin, MemoryStatus, TaskState
from harness.app import Harness


def learning_report(harness: Harness, *, limit: int = 500) -> dict[str, Any]:
    events = harness.events.query(limit=limit)
    tasks = harness.tasks.list()
    usage = harness.memory.usage_log(limit=limit)
    memories = harness.memory.list()
    too_little = (
        len(events) < int(harness.settings.get("measure.too_little_events"))
        and len(tasks) < int(harness.settings.get("measure.too_little_tasks"))
    )
    completed = sum(1 for item in tasks if item.state == TaskState.COMPLETED)
    failed = sum(1 for item in tasks if item.state == TaskState.FAILED)
    blocked = sum(1 for item in tasks if item.state == TaskState.BLOCKED)
    terminal = completed + failed + blocked
    retracted = sum(1 for item in memories if item.status == MemoryStatus.RETRACTED)
    superseded = sum(1 for item in memories if item.status == MemoryStatus.SUPERSEDED)
    inferred = [
        item
        for item in memories
        if item.origin in {MemoryOrigin.INFERRED, MemoryOrigin.CONSOLIDATED}
    ]
    valid_commits = [item for item in events if item.operation == "memory.commit"]
    invalid = [item for item in events if item.operation == "memory.reject_invalid"]
    evidence_total = len(valid_commits) + len(invalid)
    acts = [item for item in events if item.operation == "loop.act"]
    succeeded_by_task: dict[str, bool] = {}
    failed_tasks: set[str] = set()
    for item in acts:
        if item.result == "succeeded":
            succeeded_by_task[item.target] = True
        elif item.result == "failed":
            failed_tasks.add(item.target)
    recovered_tasks = {task for task in failed_tasks if succeeded_by_task.get(task)}
    included_later_retracted = sum(
        1
        for entry in usage
        if any(item.id == entry.memory_id and item.status == MemoryStatus.RETRACTED for item in memories)
    )
    inferred_retracted = sum(1 for item in inferred if item.status == MemoryStatus.RETRACTED)
    return {
        "too_little_data": too_little,
        "task_success": (completed / terminal) if terminal else None,
        "tasks_completed": completed,
        "tasks_failed": failed,
        "tasks_blocked": blocked,
        "correction_rate": ((retracted + superseded) / len(memories)) if memories else None,
        "inferred_count": len(inferred),
        "retracted_inferred": inferred_retracted,
        "inferred_retraction_rate": (inferred_retracted / len(inferred)) if inferred else None,
        "evidence_quality": (len(valid_commits) / evidence_total) if evidence_total else None,
        "recovery_rate": (len(recovered_tasks) / len(failed_tasks)) if failed_tasks else None,
        "usage_entries": len(usage),
        "retrieval_precision_proxy": (
            (len(usage) - included_later_retracted) / len(usage) if usage else None
        ),
        "included_later_retracted_proxy": included_later_retracted,
        "note": "Values are computed from stored events, tasks, traces, and the usage log. Live-eval precision is reported only by the eval suite.",
    }

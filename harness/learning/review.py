from __future__ import annotations

from harness.constants import (
    ExecutionStatus,
    LearningSignalKind,
    MemoryStatus,
    SkillStatus,
    TaskState,
)
from harness.contracts.capabilities import CapabilityExecutor
from harness.contracts.events import Event, EventLog
from harness.contracts.learning import (
    LearningPipeline,
    LearningSignal,
    ReviewItem,
    ReviewReport,
    SkillLibrary,
)
from harness.contracts.memory import MemoryManager
from harness.contracts.settings import SettingsService
from harness.contracts.tasks import TaskManager
from harness.storage.atomic import new_id, now_ts

# Harness bookkeeping operations are not user workflows worth a procedure.
EXCLUDED_CAPABILITIES = {"harness_memory", "harness_structure"}


class LearningReview:
    """Lightweight, deterministic, event-driven review of learning evidence.

    The review makes no model calls. It scans persisted evidence — capability
    attempts, tasks, pending skill candidates, stale memories — and turns
    meaningful, repeated signals into candidates through the learning
    pipeline, which enforces validation, staging, and journaling:

    - a workflow whose steps all succeeded in enough distinct tasks becomes a
      staged skill candidate (procedural learning),
    - a failure fingerprint repeated across enough distinct tasks is reported
      as a finding (the per-failure lesson already exists; the review flags
      the pattern for the user),
    - pending skill candidates are surfaced as findings awaiting review,
    - memories whose validity window has expired are surfaced as findings.

    With no meaningful new evidence it creates nothing: no candidates, no
    journal entries, no memory writes, no events. Candidates it does create
    never bypass the pipeline's evidence gate or the approval gate.
    """

    def __init__(
        self,
        *,
        settings: SettingsService,
        events: EventLog,
        tasks: TaskManager,
        capabilities: CapabilityExecutor,
        memory: MemoryManager,
        pipeline: LearningPipeline,
        skills: SkillLibrary | None = None,
    ):
        self._settings = settings
        self._events = events
        self._tasks = tasks
        self._capabilities = capabilities
        self._memory = memory
        self._pipeline = pipeline
        self._skills = skills

    def review(self, *, trigger: str) -> ReviewReport:
        report = ReviewReport(ts=now_ts(), trigger=trigger)
        attempts_by_task = self._capabilities.attempts_by_task()
        self._review_workflows(report, attempts_by_task, trigger)
        self._review_failure_clusters(report, attempts_by_task)
        self._review_pending_candidates(report)
        self._review_stale_knowledge(report)
        report.changed = bool(report.candidates)
        if report.items:
            self._events.append(
                Event(
                    id=new_id("evt"),
                    ts=now_ts(),
                    actor="harness",
                    operation="learning.review",
                    target="learning",
                    result=trigger,
                    data={
                        "items": [
                            {"kind": item.kind, "action": item.action, "detail": item.detail}
                            for item in report.items
                        ],
                        "candidates": list(report.candidates),
                        "changed": report.changed,
                    },
                )
            )
        return report

    # ---------------------------------------------------------- workflows
    def _review_workflows(self, report: ReviewReport, attempts_by_task: dict, trigger: str) -> None:
        """Find succeeded operation sequences repeated across distinct tasks.

        Only fully succeeded sequences in completed tasks count: a failed
        step invalidates the sequence as evidence, so a failed procedure can
        never be learned as a successful one.
        """
        min_repeats = int(self._settings.get("learning.min_workflow_repeats"))
        min_steps = int(self._settings.get("learning.min_workflow_steps"))
        max_candidates = int(self._settings.get("learning.max_review_candidates"))
        signatures: dict[tuple[str, ...], dict] = {}
        for task_id, attempts in attempts_by_task.items():
            try:
                task = self._tasks.get(task_id)
            except KeyError:
                continue
            if task.state != TaskState.COMPLETED:
                continue
            steps: list[str] = []
            op_refs: list[str] = []
            broken = False
            for attempt in attempts:
                if attempt.capability in EXCLUDED_CAPABILITIES:
                    continue
                if attempt.status != ExecutionStatus.SUCCEEDED:
                    # The sequence, as run, is not a verified procedure.
                    broken = True
                    break
                key = f"{attempt.capability}.{attempt.operation}"
                if not steps or steps[-1] != key:
                    steps.append(key)
                op_refs.append(attempt.operation_id)
            if broken or len(steps) < min_steps:
                continue
            signature = tuple(steps)
            entry = signatures.setdefault(signature, {"tasks": [], "goals": [], "op_refs": []})
            entry["tasks"].append(task_id)
            entry["goals"].append(task.goal)
            entry["op_refs"].extend(op_refs)
        proposed = 0
        for signature, entry in sorted(signatures.items()):
            if len(entry["tasks"]) < min_repeats:
                continue
            if proposed >= max_candidates:
                report.notes.append(
                    f"workflow {list(signature)} repeated in {len(entry['tasks'])} tasks; "
                    "candidate limit reached this review"
                )
                continue
            outcome = self._pipeline.process(
                LearningSignal(
                    kind=LearningSignalKind.REPEATED_WORKFLOW.value,
                    content=f"Workflow {list(signature)} succeeded in {len(entry['tasks'])} distinct tasks",
                    origin="scheduler" if trigger == "schedule" else "harness",
                    source_refs=list(entry["op_refs"])[:40],
                    evidence={
                        "workflow": list(signature),
                        "task_ids": list(entry["tasks"]),
                        "goals": list(entry["goals"]),
                    },
                )
            )
            if outcome.decision == "staged" and outcome.skill_id:
                proposed += 1
                report.candidates.append(outcome.skill_id)
                report.items.append(
                    ReviewItem(
                        kind="workflow",
                        action="skill_candidate",
                        detail={
                            "workflow": list(signature),
                            "tasks": len(entry["tasks"]),
                            "skill_id": outcome.skill_id,
                        },
                    )
                )
            elif outcome.decision == "duplicate":
                report.items.append(
                    ReviewItem(
                        kind="workflow",
                        action="known",
                        detail={"workflow": list(signature), "tasks": len(entry["tasks"])},
                    )
                )

    # ---------------------------------------------------- failure clusters
    def _review_failure_clusters(self, report: ReviewReport, attempts_by_task: dict) -> None:
        min_repeats = int(self._settings.get("learning.min_failure_repeats"))
        clusters: dict[tuple[str, str, str], set[str]] = {}
        for task_id, attempts in attempts_by_task.items():
            for attempt in attempts:
                if attempt.capability in EXCLUDED_CAPABILITIES:
                    continue
                if attempt.status not in {ExecutionStatus.FAILED, ExecutionStatus.DENIED}:
                    continue
                key = (
                    f"{attempt.capability}.{attempt.operation}",
                    str(attempt.status),
                    str(attempt.error_category or ""),
                )
                clusters.setdefault(key, set()).add(task_id)
        for (label, status, category), task_ids in sorted(clusters.items()):
            if len(task_ids) < min_repeats:
                continue
            report.items.append(
                ReviewItem(
                    kind="failure_cluster",
                    action="finding",
                    detail={
                        "operation": label,
                        "status": status,
                        "error_category": category,
                        "tasks": len(task_ids),
                        "note": (
                            "the per-failure lesson is already captured; this pattern is "
                            "flagged for a manual procedure or capability fix"
                        ),
                    },
                )
            )

    # ---------------------------------------------------------- candidates
    def _review_pending_candidates(self, report: ReviewReport) -> None:
        """Surface staged skill candidates as findings; approve nothing here."""
        if self._skills is None:
            return
        for record in self._skills.list(status=SkillStatus.CANDIDATE.value):
            report.items.append(
                ReviewItem(
                    kind="candidate_pending",
                    action="finding",
                    detail={
                        "skill_id": record.id,
                        "name": record.name,
                        "status": record.status,
                        "note": "awaiting explicit approval; never disclosed until approved",
                    },
                )
            )

    # ------------------------------------------------------ stale knowledge
    def _review_stale_knowledge(self, report: ReviewReport) -> None:
        now = now_ts()
        for record in self._memory.list(status=MemoryStatus.ACTIVE.value):
            until = record.valid_until
            if until and str(until) <= now:
                report.items.append(
                    ReviewItem(
                        kind="stale_knowledge",
                        action="finding",
                        detail={
                            "memory_id": record.id,
                            "valid_until": until,
                            "note": "expired validity window; already excluded from retrieval, retract to clean up",
                        },
                    )
                )

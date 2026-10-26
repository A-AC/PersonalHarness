from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from harness.constants import (
    BlockReason,
    DecisionIntent,
    DecisionStatus,
    ExecutionStatus,
    TaskState,
)
from harness.contracts.actions import ActAction, ExecutionResult
from harness.contracts.capabilities import CapabilityExecutor
from harness.contracts.control import ActionGate, ApprovedAction, ControlDirective
from harness.contracts.decisions import (
    Decision,
    DecisionInterpretation,
    DecisionResolver,
    DecisionStore,
)
from harness.contracts.domains import DomainRegistry, StructureDraft
from harness.contracts.events import Event, EventLog
from harness.contracts.session import SessionManager
from harness.contracts.settings import SettingsService
from harness.contracts.tasks import Task, TaskManager, TaskOutcome, TaskSpec
from harness.storage.atomic import new_id, now_ts
from harness.text import match_by_name


_STRUCTURE_SWITCH = re.compile(
    r"(?:please\s+)?(?:work\s+on|switch\s+to|open)\s+(?:the\s+)?(.+?)\s*[.!?]*$",
    re.IGNORECASE,
)
_STRUCTURE_CREATE = re.compile(
    r"(?:make|create|start)\s+(?:a\s+)?(?:new\s+)?domain(?:\s+(?:where|about|for|to talk about))?\s*[:\-]?\s*(.*)",
    re.IGNORECASE,
)


def _later_ts(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class DefaultControlPlane:
    """Central turn coordinator.

    The Control Plane interprets the latest input, resolves durable decisions,
    and returns a directive: handle the turn, plan new work, resume a blocked
    question, or dispatch one-time approved actions. It does not dispatch
    capabilities itself; the existing interaction loop and capability executor
    remain the only execution path.
    """

    def __init__(
        self,
        *,
        settings: SettingsService,
        sessions: SessionManager,
        tasks: TaskManager,
        domains: DomainRegistry,
        capabilities: CapabilityExecutor,
        decisions: DecisionStore,
        resolver: DecisionResolver,
        events: EventLog,
    ):
        self._settings = settings
        self._sessions = sessions
        self._tasks = tasks
        self._domains = domains
        self._capabilities = capabilities
        self._decisions = decisions
        self._resolver = resolver
        self._events = events

    # ------------------------------------------------------------ lifecycle
    def on_start(self) -> None:
        self.sync_domain_suggestions()

    def pending_decisions(self) -> list[Decision]:
        return self._decisions.pending(include_deferred=True)

    def sync_domain_suggestions(self) -> list[Decision]:
        synced = []
        for suggestion in self._domains.list_suggestions():
            synced.append(self._decisions.sync_suggestion(suggestion))
        return synced

    # ------------------------------------------------------------- turn API
    def begin_turn(self, user_text: str, interaction_id: str) -> ControlDirective:
        self.sync_domain_suggestions()
        structure = self._handle_structure_command(user_text)
        if structure is not None:
            return ControlDirective(
                mode="handled",
                intent="structure",
                messages=structure,
                reason="explicit structure command",
            )

        open_decisions = self.pending_decisions()
        waiting = [
            task
            for task in self._tasks.list(states=[TaskState.BLOCKED])
            if task.block_reason == BlockReason.AWAITING_USER
        ]
        if open_decisions:
            reading = self._resolver.interpret(
                user_text,
                decisions=open_decisions,
                awaiting_question=bool(waiting),
            )
            directive = self._from_interpretation(reading, open_decisions, user_text, interaction_id)
            if directive is not None:
                return directive

        if waiting:
            return ControlDirective(
                mode="resume",
                intent="answer",
                task_ids=[task.id for task in waiting],
                reason="latest message answers a pending question",
            )

        if self._is_cancel(user_text):
            return self._cancel_without_decision(user_text)
        return ControlDirective(mode="plan", intent="request", reason="new request")

    def _from_interpretation(
        self,
        reading: DecisionInterpretation,
        decisions: list[Decision],
        user_text: str,
        interaction_id: str,
    ) -> ControlDirective | None:
        intent = str(reading.intent)
        if intent == DecisionIntent.UNRELATED.value:
            return ControlDirective(
                mode="plan",
                intent=intent,
                decision_ids=reading.decision_ids,
                reason="unrelated request while a decision is pending",
            )
        if intent == DecisionIntent.NONE.value:
            return None
        if intent == DecisionIntent.CLARIFY.value:
            ids = reading.decision_ids or [item.id for item in decisions]
            return ControlDirective(
                mode="handled",
                intent=intent,
                decision_ids=ids,
                messages=[self._clarification_message(decisions, reading.selected_id)],
                reason=reading.reason or "ambiguous reply",
            )

        selected = self._selected_decision(reading, decisions)
        if selected is None:
            if intent in {
                DecisionIntent.APPROVE.value,
                DecisionIntent.REJECT.value,
                DecisionIntent.DEFER.value,
                DecisionIntent.MODIFY.value,
            }:
                return ControlDirective(
                    mode="handled",
                    intent=intent,
                    decision_ids=[item.id for item in decisions],
                    messages=[self._ambiguity_message(decisions)],
                    reason="multiple applicable decisions",
                )
            if intent == DecisionIntent.CANCEL.value:
                return self._cancel_without_decision(user_text)

        if selected is None:
            return ControlDirective(
                mode="handled",
                intent=intent,
                messages=[self._ambiguity_message(decisions)],
                reason="no applicable decision",
            )

        if intent == DecisionIntent.APPROVE.value:
            return self._approve(selected, interaction_id)
        if intent == DecisionIntent.REJECT.value:
            return self._reject(selected, interaction_id)
        if intent == DecisionIntent.DEFER.value:
            return self._defer(selected, interaction_id)
        if intent == DecisionIntent.MODIFY.value:
            return self._modify(selected, reading, interaction_id)
        if intent == DecisionIntent.CANCEL.value:
            return self._cancel_decision(selected, interaction_id)
        return None

    @staticmethod
    def _selected_decision(reading: DecisionInterpretation, decisions: list[Decision]) -> Decision | None:
        if reading.selected_id:
            for item in decisions:
                if item.id == reading.selected_id:
                    return item
        if len(decisions) == 1:
            return decisions[0]
        return None

    # --------------------------------------------------------- resolutions
    def _approve(self, decision: Decision, interaction_id: str) -> ControlDirective:
        decision.status = DecisionStatus.APPROVED.value
        decision.resolution = "approved"
        decision.resolution_text = "User approved this one-time action."
        decision.resolved_at = now_ts()
        decision.resolution_interaction_id = interaction_id
        decision = self._decisions.update(decision)
        self._record_resolution(decision, "approved", interaction_id)

        if decision.kind == "domain_creation":
            message = self._confirm_domain_decision(decision)
            return ControlDirective(
                mode="handled",
                intent="approve",
                messages=[message],
                decision_ids=[decision.id],
            )

        task_id = decision.task_id
        if task_id:
            task = self._get_task(task_id)
            if task is None or task.state in {
                TaskState.CANCELLED,
                TaskState.COMPLETED,
                TaskState.FAILED,
            }:
                decision.status = DecisionStatus.FAILED.value
                decision.resolution_text = "The linked task is no longer active."
                decision.consumed_at = now_ts()
                self._decisions.update(decision)
                return ControlDirective(
                    mode="handled",
                    intent="approve",
                    messages=[f"I couldn't run {decision.summary}: the linked task is no longer active."],
                    decision_ids=[decision.id],
                )

        if decision.kind == "reconciliation":
            return self._confirm_reconciliation(decision, interaction_id)

        if decision.kind == "plan":
            steps = [
                step
                for step in (decision.proposal.get("steps") or [])
                if isinstance(step, dict)
            ]
            if not steps:
                decision.status = DecisionStatus.CANCELLED.value
                decision.resolution_text = "Approved plan had no remaining actions."
                decision.consumed_at = now_ts()
                self._decisions.update(decision)
                return ControlDirective(
                    mode="handled",
                    intent="approve",
                    messages=["There was no remaining action to run."],
                    decision_ids=[decision.id],
                )
            actions = []
            for step in steps:
                action_task_id = str(step.get("task_id") or task_id or "") or None
                actions.append(
                    ApprovedAction(
                        decision_id=decision.id,
                        task_id=action_task_id,
                        proposal={
                            "capability": step.get("capability"),
                            "operation": step.get("operation"),
                            "arguments": dict(step.get("arguments") or {}),
                            "goal": step.get("goal"),
                            "requires_confirmation": False,
                        },
                    )
                )
            return ControlDirective(
                mode="execute",
                intent="approve",
                messages=[],
                decision_ids=[decision.id],
                task_ids=[decision.task_id] if decision.task_id else [],
                actions=actions,
                reason="approved plan",
            )

        proposal = dict(decision.proposal)
        task_id = task_id or str(proposal.get("task_id") or "") or None
        if task_id is None:
            # A decision created manually can still be approved safely; it gets
            # a task so execution, evidence, and outcomes use the normal path.
            task = self._tasks.create(
                TaskSpec(
                    goal=decision.summary,
                    domain_id=decision.domain_id,
                    interaction_id=interaction_id,
                )
            )
            task_id = task.id
            decision.task_id = task.id
            decision.scope.setdefault("task_ids", [task.id])
            self._decisions.update(decision)
        return ControlDirective(
            mode="execute",
            intent="approve",
            messages=[],
            decision_ids=[decision.id],
            task_ids=[task_id],
            actions=[
                ApprovedAction(
                    decision_id=decision.id,
                    task_id=task_id,
                    proposal=proposal,
                )
            ],
            reason="approved action",
        )

    def _reject(self, decision: Decision, interaction_id: str) -> ControlDirective:
        decision.status = DecisionStatus.REJECTED.value
        decision.resolution = "rejected"
        decision.resolution_text = "User rejected this proposal; it was not executed."
        decision.resolved_at = now_ts()
        decision.resolution_interaction_id = interaction_id
        decision = self._decisions.update(decision)
        self._record_resolution(decision, "rejected", interaction_id)

        if decision.kind == "domain_creation":
            suggestion_id = str(decision.proposal.get("suggestion_id") or "")
            if suggestion_id:
                try:
                    self._domains.decline_suggestion(suggestion_id)
                except (KeyError, ValueError):
                    pass
            message = "Okay, I won't create that domain."
        elif decision.kind == "reconciliation":
            return self._reject_reconciliation(decision, interaction_id)
        else:
            if decision.task_id and not self._has_other_open(decision):
                self._cancel_task(
                    decision.task_id,
                    f"Rejected pending decision: {decision.summary}",
                    interaction_id,
                )
            message = (
                f"Okay, I won't run {decision.summary}. "
                "No action was executed."
            )
        return ControlDirective(
            mode="handled",
            intent="reject",
            messages=[message],
            decision_ids=[decision.id],
        )

    def _defer(self, decision: Decision, interaction_id: str) -> ControlDirective:
        decision.status = DecisionStatus.DEFERRED.value
        decision.resolution = "deferred"
        decision.resolution_text = "User deferred this proposal."
        decision.resolved_at = now_ts()
        decision.resolution_interaction_id = interaction_id
        decision = self._decisions.update(decision)
        self._record_resolution(decision, "deferred", interaction_id)
        defer_note = ""
        if decision.task_id and decision.kind != "domain_creation":
            task = self._get_task(decision.task_id)
            if task is not None and task.state not in {
                TaskState.CANCELLED,
                TaskState.COMPLETED,
                TaskState.FAILED,
            }:
                # The deferred decision still holds the task: the scheduler
                # will not release it while the decision stays open, so a
                # later "yes" (not the clock) resumes the work.
                try:
                    hours = float(self._settings.get("scheduler.default_defer_hours"))
                    until = None
                    if hours > 0:
                        until = _later_ts(hours * 3600.0)
                    self._tasks.defer(
                        task.id,
                        until,
                        reason="user deferred the pending proposal",
                    )
                    defer_note = (
                        f" The work stays paused until you answer (window: {until or 'indefinite'})."
                        if until
                        else " The work stays paused until you answer."
                    )
                except ValueError:
                    pass
        return ControlDirective(
            mode="handled",
            intent="defer",
            messages=[
                f"Deferred: {decision.summary}. I haven't run it; tell me when you want to proceed."
                + defer_note
            ],
            decision_ids=[decision.id],
        )

    def _cancel_decision(self, decision: Decision, interaction_id: str) -> ControlDirective:
        decision.status = DecisionStatus.CANCELLED.value
        decision.resolution = "cancelled"
        decision.resolution_text = "User cancelled this proposal; it was not executed."
        decision.resolved_at = now_ts()
        decision.resolution_interaction_id = interaction_id
        decision = self._decisions.update(decision)
        self._record_resolution(decision, "cancelled", interaction_id)
        if decision.kind == "domain_creation":
            suggestion_id = str(decision.proposal.get("suggestion_id") or "")
            if suggestion_id:
                try:
                    self._domains.decline_suggestion(suggestion_id)
                except (KeyError, ValueError):
                    pass
        elif decision.task_id and not self._has_other_open(decision):
            self._cancel_task(
                decision.task_id,
                f"Cancelled pending decision: {decision.summary}",
                interaction_id,
            )
        return ControlDirective(
            mode="handled",
            intent="cancel",
            messages=[f"Cancelled: {decision.summary}. I did not run it."],
            decision_ids=[decision.id],
        )

    def _modify(
        self,
        decision: Decision,
        reading: DecisionInterpretation,
        interaction_id: str,
    ) -> ControlDirective:
        modification = reading.modification or {"text": "", "exclusions": []}
        exclusions = [str(item) for item in modification.get("exclusions") or [] if str(item).strip()]
        if decision.kind == "domain_creation":
            return ControlDirective(
                mode="handled",
                intent="modify",
                messages=[
                    "I need the exact change for that domain proposal. "
                    "The domain decision is still pending and nothing was created."
                ],
                decision_ids=[decision.id],
            )
        if not exclusions:
            return ControlDirective(
                mode="handled",
                intent="modify",
                messages=[
                    "I couldn't turn that into a safe change to the proposal. "
                    f"{decision.summary} is still pending; tell me which part to change."
                ],
                decision_ids=[decision.id],
            )

        proposal = copy.deepcopy(decision.proposal)
        if decision.kind == "plan" or proposal.get("type") == "plan":
            original_steps = [step for step in proposal.get("steps") or [] if isinstance(step, dict)]
            kept = [
                step for step in original_steps if not self._step_matches_any(step, exclusions)
            ]
            if not kept:
                decision.status = DecisionStatus.CANCELLED.value
                decision.resolution = "modified"
                decision.resolution_text = (
                    "User approved with an exclusion that removed every proposed action; "
                    "nothing was executed."
                )
                decision.resolved_at = now_ts()
                decision.resolution_interaction_id = interaction_id
                decision.proposal = proposal
                self._decisions.update(decision)
                self._record_resolution(decision, "modified", interaction_id)
                if decision.task_id and not self._has_other_open(decision):
                    self._cancel_task(
                        decision.task_id,
                        "Approved proposal had no remaining action after modification.",
                        interaction_id,
                    )
                return ControlDirective(
                    mode="handled",
                    intent="modify",
                    messages=[
                        f"Understood. I removed {', '.join(exclusions)}; "
                        "no remaining action was executed."
                    ],
                    decision_ids=[decision.id],
                )
            proposal["steps"] = kept
            proposal["modifications"] = list(proposal.get("modifications") or []) + exclusions
            errors = self._validate_plan(proposal)
            if errors:
                return self._invalid_modification(decision, errors)
            decision.proposal = proposal
            decision.summary = self._plan_summary(proposal)
            decision.status = DecisionStatus.APPROVED.value
            decision.resolution = "modified"
            decision.resolution_text = "User approved the proposal with a modification."
            decision.resolved_at = now_ts()
            decision.resolution_interaction_id = interaction_id
            self._decisions.update(decision)
            self._record_resolution(decision, "modified", interaction_id)
            actions = [
                ApprovedAction(
                    decision_id=decision.id,
                    task_id=str(step.get("task_id") or decision.task_id or "") or None,
                    proposal={
                        "capability": step.get("capability"),
                        "operation": step.get("operation"),
                        "arguments": dict(step.get("arguments") or {}),
                        "goal": step.get("goal"),
                        "requires_confirmation": False,
                    },
                )
                for step in kept
            ]
            return ControlDirective(
                mode="execute",
                intent="modify",
                messages=[],
                decision_ids=[decision.id],
                task_ids=[decision.task_id] if decision.task_id else [],
                actions=actions,
                reason="modified approval",
            )

        arguments = dict(proposal.get("arguments") or {})
        removed: list[str] = []
        for key, value in list(arguments.items()):
            haystack = f"{key} {value}".replace("_", " ").replace("-", " ")
            if self._matches_any(haystack, exclusions):
                arguments.pop(key)
                removed.append(key)
        if removed:
            proposal["arguments"] = arguments
            proposal["modifications"] = list(proposal.get("modifications") or []) + exclusions
            errors = self._validate_action(proposal)
            if errors:
                return self._invalid_modification(decision, errors)
            decision.proposal = proposal
            decision.summary = self._action_summary(proposal)
            decision.status = DecisionStatus.APPROVED.value
            decision.resolution = "modified"
            decision.resolution_text = "User approved with modified arguments."
            decision.resolved_at = now_ts()
            decision.resolution_interaction_id = interaction_id
            self._decisions.update(decision)
            self._record_resolution(decision, "modified", interaction_id)
            return ControlDirective(
                mode="execute",
                intent="modify",
                messages=[],
                decision_ids=[decision.id],
                task_ids=[decision.task_id] if decision.task_id else [],
                actions=[
                    ApprovedAction(
                        decision_id=decision.id,
                        task_id=decision.task_id,
                        proposal=proposal,
                    )
                ],
                reason="modified approval",
            )

        haystack = " ".join(
            [
                str(proposal.get("capability") or ""),
                str(proposal.get("operation") or ""),
                json.dumps(arguments, ensure_ascii=False),
            ]
        ).replace("_", " ").replace("-", " ")
        if self._matches_any(haystack, exclusions):
            decision.status = DecisionStatus.CANCELLED.value
            decision.resolution = "modified"
            decision.resolution_text = (
                "User excluded the entire proposed action; it was not executed."
            )
            decision.resolved_at = now_ts()
            decision.resolution_interaction_id = interaction_id
            decision.proposal = proposal
            self._decisions.update(decision)
            self._record_resolution(decision, "modified", interaction_id)
            if decision.task_id and not self._has_other_open(decision):
                self._cancel_task(
                    decision.task_id,
                    "Proposed action was excluded by the user.",
                    interaction_id,
                )
            return ControlDirective(
                mode="handled",
                intent="modify",
                messages=[
                    f"Understood. I won't run {decision.summary}; nothing was executed."
                ],
                decision_ids=[decision.id],
            )

        return ControlDirective(
            mode="handled",
            intent="modify",
            messages=[
                "I couldn't match that change to the pending proposal. "
                f"{decision.summary} is still pending and nothing was executed."
            ],
            decision_ids=[decision.id],
        )

    def _invalid_modification(self, decision: Decision, errors: list[str]) -> ControlDirective:
        return ControlDirective(
            mode="handled",
            intent="modify",
            messages=[
                "I couldn't validate that change, so I did not run it: "
                + "; ".join(errors)
                + f". {decision.summary} is still pending."
            ],
            decision_ids=[decision.id],
        )

    # ---------------------------------------------------- reconciliation
    def _confirm_reconciliation(self, decision: Decision, interaction_id: str) -> ControlDirective:
        """The user confirmed an unknown-outcome attempt actually completed.

        No execution happens here. The user's answer is the evidence; the
        task is closed honestly with that provenance recorded.
        """
        proposal = decision.proposal or {}
        operation_id = str(proposal.get("operation_id") or "")
        label = str(proposal.get("capability") or "?") + "." + str(proposal.get("operation") or "?")
        message = f"Understood: you confirmed {label} completed. I've recorded that as the outcome."
        task = self._get_task(decision.task_id) if decision.task_id else None
        if task is not None and task.state not in {
            TaskState.CANCELLED,
            TaskState.COMPLETED,
        }:
            self._tasks.record_checkpoint(
                task.id,
                "User confirmed the interrupted action completed; outcome recorded without re-execution.",
                {"operation_id": operation_id},
            )
            self._tasks.mark_succeeded_execution(task.id)
            try:
                self._tasks.transition(task.id, TaskState.COMPLETED, reason=None)
                self._tasks.record_outcome(
                    task.id,
                    TaskOutcome(
                        summary=f"User confirmed {label} completed after an unknown-outcome interruption.",
                        status=TaskState.COMPLETED.value,
                        data={"confirmed_by": "user", "operation_id": operation_id},
                    ),
                )
            except ValueError:
                pass
        else:
            message = f"Thanks — I recorded that {label} completed. The linked task is no longer active."
        decision.status = DecisionStatus.COMPLETED.value
        decision.resolution = "confirmed"
        decision.resolution_text = "User confirmed the interrupted action completed; it was not re-executed."
        decision.consumed_at = now_ts()
        self._decisions.update(decision)
        return ControlDirective(
            mode="handled",
            intent="approve",
            messages=[message],
            decision_ids=[decision.id],
        )

    def _reject_reconciliation(self, decision: Decision, interaction_id: str) -> ControlDirective:
        """The user says the unknown-outcome attempt did not complete.

        The work is requeued (bounded by the scheduler's retry budget), never
        blindly repeated: the next attempt goes through the normal loop and
        its own confirmation gates.
        """
        proposal = decision.proposal or {}
        label = str(proposal.get("capability") or "?") + "." + str(proposal.get("operation") or "?")
        message = (
            f"Understood — {label} did not complete. I'll treat it as unfinished; "
            "it stays queued and any retry still goes through the normal confirmation checks."
        )
        task = self._get_task(decision.task_id) if decision.task_id else None
        if task is not None and task.state in {
            TaskState.BLOCKED,
            TaskState.DEFERRED,
            TaskState.FAILED,
        }:
            try:
                self._tasks.resume(task.id, reason="user said the unknown-outcome attempt did not complete")
                self._tasks.record_checkpoint(
                    task.id,
                    "User said the interrupted action did not complete; requeued.",
                    {"operation_id": str(proposal.get("operation_id") or "")},
                )
            except ValueError:
                message = f"Understood — {label} did not complete. The linked task is no longer resumable."
        return ControlDirective(
            mode="handled",
            intent="reject",
            messages=[message],
            decision_ids=[decision.id],
        )

    # --------------------------------------------------------- confirmation
    def gate_action(
        self,
        proposal: ActAction,
        task: Task,
        interaction_id: str,
    ) -> ActionGate:
        if not self._confirmation_required(proposal):
            return ActionGate(allowed=True)
        existing = self._decisions.find_for_task(task.id, open_only=True)
        if existing is not None and existing.is_open:
            return ActionGate(
                allowed=False,
                decision=existing,
                prompt=existing.prompt,
            )
        decision = self._create_action_decision(
            proposal=proposal,
            task=task,
            interaction_id=interaction_id,
            prompt=None,
        )
        return ActionGate(allowed=False, decision=decision, prompt=decision.prompt)

    def _confirmation_required(self, proposal: ActAction) -> bool:
        if bool(proposal.requires_confirmation):
            return True
        if not bool(self._settings.get("control.confirmation.enabled")):
            return False
        permissions = self._confirmation_permissions()
        if "all" in permissions:
            return True
        try:
            manifest = self._capabilities.get(proposal.capability)
        except KeyError:
            return False
        for op in manifest.operations:
            if op.name != proposal.operation:
                continue
            if op.requires_confirmation:
                return True
            return str(op.permission) in permissions
        return False

    def _confirmation_permissions(self) -> set[str]:
        raw = str(self._settings.get("control.confirmation.permissions") or "")
        return {item.strip().lower() for item in raw.split(",") if item.strip()}

    def _create_action_decision(
        self,
        *,
        proposal: ActAction,
        task: Task,
        interaction_id: str,
        prompt: str | None,
    ) -> Decision:
        permission = self._operation_permission(proposal)
        # A model-supplied risk label may add detail, but it cannot downgrade a
        # destructive operation below its manifest permission.
        risk = (
            "destructive"
            if permission == "destructive"
            else (proposal.risk or permission or "unknown")
        )
        payload = {
            "type": "capability_action",
            "capability": proposal.capability,
            "operation": proposal.operation,
            "arguments": dict(proposal.arguments),
            "requires_confirmation": True,
            "risk": risk,
            "task_id": task.id,
        }
        summary = self._action_summary(payload)
        asked = prompt or (
            f"I need confirmation before running {proposal.capability}.{proposal.operation} "
            f"({risk}). Should I proceed?"
        )
        ts = now_ts()
        decision = Decision(
            id=new_id("dec"),
            kind="action",
            status=DecisionStatus.PENDING.value,
            created_at=ts,
            updated_at=ts,
            prompt=asked,
            summary=summary,
            proposal=payload,
            scope={"task_ids": [task.id], "domain_id": task.domain_id, "risk": risk},
            risk=risk,
            task_id=task.id,
            interaction_id=interaction_id,
            domain_id=task.domain_id,
            source="model",
        )
        return self._decisions.create(decision)

    def propose_action(
        self,
        task_id: str,
        capability: str,
        operation: str,
        arguments: dict[str, Any] | None = None,
        *,
        interaction_id: str | None = None,
        prompt: str | None = None,
        risk: str | None = None,
    ) -> Decision:
        task = self._tasks.get(task_id)
        decision = self._create_action_decision(
            proposal=ActAction(
                capability=capability,
                operation=operation,
                arguments=dict(arguments or {}),
                requires_confirmation=True,
                risk=risk,
            ),
            task=task,
            interaction_id=interaction_id or "",
            prompt=prompt,
        )
        self._block_task(task, decision)
        return decision

    def propose_plan(
        self,
        steps: list[dict[str, Any]],
        *,
        task_id: str | None = None,
        interaction_id: str | None = None,
        prompt: str | None = None,
        risk: str | None = None,
    ) -> Decision:
        payload = {
            "type": "plan",
            "steps": [copy.deepcopy(step) for step in steps],
            "task_id": task_id,
        }
        summary = self._plan_summary(payload)
        plan_risk = self._plan_risk(payload.get("steps") or [], risk)
        ts = now_ts()
        decision = Decision(
            id=new_id("dec"),
            kind="plan",
            status=DecisionStatus.PENDING.value,
            created_at=ts,
            updated_at=ts,
            prompt=prompt or f"I need confirmation before running this plan: {summary}. Proceed?",
            summary=summary,
            proposal=payload,
            scope={"task_ids": [task_id] if task_id else [], "risk": plan_risk},
            risk=plan_risk,
            task_id=task_id,
            interaction_id=interaction_id,
            source="harness",
        )
        self._decisions.create(decision)
        if task_id:
            self._block_task(self._tasks.get(task_id), decision)
        return decision

    def _block_task(self, task: Task, decision: Decision) -> None:
        if task.state in {
            TaskState.PENDING,
            TaskState.READY,
            TaskState.RUNNING,
        }:
            self._tasks.transition(task.id, TaskState.BLOCKED, reason=BlockReason.AWAITING_CONFIRMATION)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=now_ts(),
                actor="harness",
                operation="decision.block_task",
                target=task.id,
                result=BlockReason.AWAITING_CONFIRMATION.value,
                trigger_ref=decision.id,
            )
        )

    def _confirm_domain_decision(self, decision: Decision) -> str:
        suggestion_id = str(decision.proposal.get("suggestion_id") or "")
        try:
            created = self._domains.confirm_suggestion(suggestion_id)
        except (KeyError, ValueError) as exc:
            decision.status = DecisionStatus.FAILED.value
            decision.resolution_text = f"Domain suggestion could not be confirmed: {exc}"
            decision.consumed_at = now_ts()
            self._decisions.update(decision)
            return f"I couldn't create that domain: {exc}"
        decision.status = DecisionStatus.COMPLETED.value
        decision.consumed_at = now_ts()
        decision.resolution_text = f"Created domain '{created.name}' ({created.id})."
        self._decisions.update(decision)
        return f"Created domain '{created.name}' ({created.id})."

    # -------------------------------------------------------- execution log
    def record_execution(
        self,
        decision_id: str,
        execution: ExecutionResult,
        *,
        task_id: str | None = None,
        final: bool = True,
    ) -> Decision | None:
        try:
            decision = self._decisions.get(decision_id)
        except KeyError:
            return None
        entry = {
            "operation_id": execution.operation_id,
            "capability": execution.capability,
            "operation": execution.operation,
            "status": execution.status,
            "message": execution.message,
            "error_category": execution.error_category,
            "task_id": task_id or execution.task_id,
        }
        history = decision.metadata.setdefault("executions", [])
        if isinstance(history, list):
            history.append(entry)
        decision.operation_id = execution.operation_id
        if final:
            decision.consumed_at = now_ts()
            if execution.status == ExecutionStatus.SUCCEEDED:
                decision.status = DecisionStatus.COMPLETED.value
                decision.resolution_text = f"Approved action completed: {execution.message}"
            else:
                decision.status = DecisionStatus.FAILED.value
                decision.resolution_text = (
                    f"Approved action did not succeed ({execution.status}): {execution.message}"
                )
        self._decisions.update(decision)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=now_ts(),
                actor="harness",
                operation="decision.execution",
                target=decision.id,
                result=str(execution.status),
                refs=[item for item in [execution.operation_id, task_id or execution.task_id] if item],
                data=entry,
            )
        )
        return decision

    # ----------------------------------------------------------- cancellation
    def _cancel_without_decision(self, user_text: str) -> ControlDirective:
        targets = self._cancellation_targets(user_text)
        if targets is None:
            return ControlDirective(
                mode="handled",
                intent="cancel",
                messages=[self._unscoped_cancel_message()],
                reason="ambiguous cancellation scope",
            )
        if not targets:
            return ControlDirective(
                mode="handled",
                intent="cancel",
                messages=["There is no active work to cancel."],
            )
        for task in targets:
            self._cancel_task(task.id, "Cancelled by user.", "")
        return ControlDirective(
            mode="handled",
            intent="cancel",
            messages=[
                "Cancelled: " + ", ".join(task.goal for task in targets) + ".",
            ],
            task_ids=[task.id for task in targets],
        )

    def _cancellation_targets(self, user_text: str) -> list[Task] | None:
        active = self._tasks.list(
            states=[TaskState.PENDING, TaskState.READY, TaskState.RUNNING, TaskState.BLOCKED]
        )
        if not active:
            return []
        lowered = user_text.lower()
        named = [task for task in active if task.id.lower() in lowered]
        if len(named) == 1:
            return named
        words = {
            item
            for item in re.findall(r"[a-z0-9]+", lowered)
            if len(item) > 3 and item not in {"cancel", "stop", "that", "this", "task", "work"}
        }
        scored = []
        for task in active:
            goal_words = set(re.findall(r"[a-z0-9]+", task.goal.lower()))
            overlap = len(words & goal_words)
            if overlap:
                scored.append((overlap, task))
        if scored:
            best = max(item[0] for item in scored)
            winners = [task for score, task in scored if score == best]
            if len(winners) == 1:
                return winners
        if len(active) == 1:
            return active
        return None

    def _cancel_task(self, task_id: str, message: str, interaction_id: str) -> Task | None:
        task = self._get_task(task_id)
        if task is None or task.state in {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }:
            return task
        try:
            task = self._tasks.transition(task_id, TaskState.CANCELLED, reason=None)
        except ValueError:
            return task
        self._tasks.mark_delivered(task.id, message)
        self._tasks.record_outcome(
            task.id, TaskOutcome(summary=message, status=TaskState.CANCELLED.value)
        )
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=now_ts(),
                actor="harness",
                operation="task.cancel",
                target=task.id,
                result=TaskState.CANCELLED.value,
                trigger_ref=interaction_id or None,
                data={"reason": message},
            )
        )
        return task

    def _unscoped_cancel_message(self) -> str:
        active = self._tasks.list(
            states=[TaskState.PENDING, TaskState.READY, TaskState.RUNNING, TaskState.BLOCKED]
        )
        if not active:
            return "There is no active work to cancel."
        lines = [f"- {task.id}: {task.goal}" for task in active]
        return (
            "I need a narrower cancellation target; I won't cancel unrelated work. "
            "Active tasks:\n" + "\n".join(lines)
        )

    @staticmethod
    def _is_cancel(user_text: str) -> bool:
        normalized = re.sub(r"\s+", " ", user_text.strip().lower()).strip(" .!?,;:")
        return normalized in {
            "stop", "stop that", "stop it", "cancel", "cancel that", "cancel it",
            "abort", "forget it", "never mind", "stop the task", "stop the work",
        } or normalized.startswith(("cancel ", "stop "))

    # ------------------------------------------------------ structure inputs
    def _handle_structure_command(self, user_text: str) -> list[str] | None:
        text = user_text.strip()
        lowered = text.lower()
        switched = _STRUCTURE_SWITCH.match(text)
        if switched:
            name = switched.group(1).strip().strip("\"'")
            domain, candidates = match_by_name(self._domains.list_domains(), name)
            if domain is not None:
                self._sessions.set_active_domain(domain.id)
                return [f"Active domain: '{domain.name}' ({domain.id})."]
            if candidates:
                return [
                    f"I found more than one domain for '{name}': "
                    f"{', '.join(candidates)}. Please use the exact name."
                ]
            # Unknown names fall through: ordinary prose such as "work on the
            # parser bug" is a request, not a failed domain switch.
            return None
        match = _STRUCTURE_CREATE.search(lowered)
        if match:
            name = text[match.start(1) :].strip().strip(".?!\"'")
            name = re.sub(
                r"^(?:where\s+)?(?:we(?:'ll| will)?\s+(?:talk|work)\s+(?:about|on)\s+)",
                "",
                name,
            ).strip()
            name = re.sub(r"^(?:about|for|on)\s+", "", name).strip()
            if not name:
                return ["What should the domain be about?"]
            draft = StructureDraft(
                name=name,
                reason="explicit user command",
                evidence=f"user said: {text}",
            )
            created = self._domains.create_domain(draft, confirmed=True)
            return [
                f"Created domain '{created.name}' ({created.id}). "
                "You can rename or archive it in the UI."
            ]
        return None

    # ------------------------------------------------------------- messages
    def _ambiguity_message(self, decisions: list[Decision]) -> str:
        lines = [f"{idx}. {item.summary} ({item.risk})" for idx, item in enumerate(decisions, 1)]
        return (
            "I have more than one pending decision, so I won't guess which one you mean:\n"
            + "\n".join(lines)
            + "\nWhich one?"
        )

    def _clarification_message(self, decisions: list[Decision], selected_id: str | None) -> str:
        chosen = [item for item in decisions if item.id == selected_id] or decisions
        if len(chosen) == 1:
            item = chosen[0]
            return (
                f"I haven't acted yet. The pending decision is: {item.summary} "
                f"({item.risk}). Reply yes to approve it, no to reject it, or tell me how to change it."
            )
        return self._ambiguity_message(chosen)

    # -------------------------------------------------------------- helpers
    def _get_task(self, task_id: str) -> Task | None:
        try:
            return self._tasks.get(task_id)
        except KeyError:
            return None

    def _has_other_open(self, decision: Decision) -> bool:
        if not decision.task_id:
            return False
        for item in self._decisions.list():
            if item.id == decision.id or not item.is_open:
                continue
            if decision.task_id in item.task_ids:
                return True
        return False

    def _record_resolution(self, decision: Decision, result: str, interaction_id: str) -> None:
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=now_ts(),
                actor="user",
                operation="decision.resolve",
                target=decision.id,
                result=result,
                trigger_ref=interaction_id,
                refs=[item for item in [decision.task_id] if item],
                data={"kind": decision.kind, "status": decision.status},
            )
        )

    @staticmethod
    def _action_summary(proposal: dict[str, Any]) -> str:
        capability = str(proposal.get("capability") or "unknown")
        operation = str(proposal.get("operation") or "unknown")
        arguments = proposal.get("arguments") or {}
        if isinstance(arguments, dict) and arguments:
            rendered = ", ".join(f"{key}={value}" for key, value in arguments.items())
            if len(rendered) > 160:
                rendered = rendered[:157] + "..."
            return f"run {capability}.{operation} ({rendered})"
        return f"run {capability}.{operation}"

    @staticmethod
    def _plan_summary(proposal: dict[str, Any]) -> str:
        steps = [step for step in proposal.get("steps") or [] if isinstance(step, dict)]
        rendered = []
        for step in steps:
            label = step.get("goal")
            if not label:
                label = f"{step.get('capability', '?')}.{step.get('operation', '?')}"
            rendered.append(str(label))
        return "plan: " + " -> ".join(rendered) if rendered else "empty plan"

    def _operation_permission(self, proposal: ActAction) -> str | None:
        try:
            manifest = self._capabilities.get(proposal.capability)
        except KeyError:
            return None
        for op in manifest.operations:
            if op.name == proposal.operation:
                return str(op.permission)
        return None

    def _plan_risk(self, steps: list[dict[str, Any]], declared: str | None) -> str:
        permissions: set[str] = set()
        for step in steps:
            if not isinstance(step, dict):
                continue
            capability = str(step.get("capability") or "")
            operation = str(step.get("operation") or "")
            try:
                manifest = self._capabilities.get(capability)
            except KeyError:
                continue
            for op in manifest.operations:
                if op.name == operation:
                    permissions.add(str(op.permission))
                    break
        if "destructive" in permissions:
            return "destructive"
        if "write" in permissions and (declared is None or declared in {"unknown", "read"}):
            return "write"
        return declared or "unknown"

    def _validate_action(self, proposal: dict[str, Any]) -> list[str]:
        capability = str(proposal.get("capability") or "")
        operation = str(proposal.get("operation") or "")
        arguments = proposal.get("arguments") or {}
        result = self._capabilities.validate_arguments(capability, operation, arguments)
        if result.ok:
            proposal["arguments"] = result.normalized_arguments
        return list(result.errors)

    def _validate_plan(self, proposal: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for index, step in enumerate(proposal.get("steps") or [], 1):
            if not isinstance(step, dict):
                errors.append(f"step {index} is not an object")
                continue
            step_errors = self._validate_action(step)
            errors.extend(f"step {index}: {item}" for item in step_errors)
        return errors

    @staticmethod
    def _step_matches_any(step: dict[str, Any], exclusions: list[str]) -> bool:
        haystack = " ".join(
            [
                str(step.get("capability") or ""),
                str(step.get("operation") or ""),
                str(step.get("goal") or ""),
                str(step.get("summary") or ""),
                json.dumps(step.get("arguments") or {}, ensure_ascii=False),
            ]
        )
        return DefaultControlPlane._matches_any(haystack, exclusions)

    @staticmethod
    def _matches_any(haystack: str, exclusions: list[str]) -> bool:
        normalized = re.sub(r"[_\-]+", " ", haystack.lower())
        for phrase in exclusions:
            tokens = [
                item
                for item in re.findall(r"[a-z0-9]+", phrase.lower())
                if item not in {"the", "a", "an", "that", "this", "it", "please", "do", "not"}
            ]
            if not tokens:
                continue
            if all(token in normalized for token in tokens):
                return True
        return False

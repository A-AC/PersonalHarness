from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from typing import Any

from harness.constants import SELF_DOMAIN_ID, ActionKind, BlockReason, ExecutionStatus, PermissionLevel, TaskState
from harness.contracts.actions import ActAction, ExecutionResult, Proposal
from harness.contracts.capabilities import CapabilityExecutor, ExecuteRequest
from harness.contracts.context import ContextBuilder
from harness.contracts.domains import DomainRegistry, StructureDraft
from harness.contracts.events import Event, EventLog
from harness.contracts.loop import TurnResult
from harness.contracts.model import Message, ModelAdapter, ModelUnavailable
from harness.contracts.session import SessionManager
from harness.contracts.settings import SettingsService
from harness.contracts.tasks import Task, TaskManager, TaskOutcome, TaskSpec
from harness.contracts.traces import Trace, TraceStore
from harness.orchestrator.machine import apply_action
from harness.orchestrator.parse import PROPOSAL_SCHEMA, TASK_PLAN_SCHEMA, parse_proposal, parse_task_plan
from harness.prompts import TASK_PLAN_INSTRUCTIONS
from harness.storage.atomic import new_id, now_ts


class DefaultInteractionLoop:
    def __init__(
        self,
        settings: SettingsService,
        model: ModelAdapter,
        sessions: SessionManager,
        tasks: TaskManager,
        domains: DomainRegistry,
        context: ContextBuilder,
        capabilities: CapabilityExecutor,
        traces: TraceStore,
        events: EventLog,
    ):
        self._settings = settings
        self._model = model
        self._sessions = sessions
        self._tasks = tasks
        self._domains = domains
        self._context = context
        self._capabilities = capabilities
        self._traces = traces
        self._events = events

    def handle_turn(self, user_text: str) -> TurnResult:
        interaction = self._sessions.record_interaction(user_text)
        messages: list[str] = []
        structure = self._handle_structure_command(user_text)
        if structure is not None:
            messages.extend(structure)
            text = "\n".join(messages)
            self._sessions.append_assistant(interaction.id, text, task_ids=[])
            return TurnResult(interaction_id=interaction.id, messages=messages, tasks=self._tasks.list())

        waiting = [
            task
            for task in self._tasks.list(states=[TaskState.BLOCKED])
            if task.block_reason == BlockReason.AWAITING_USER
        ]
        if waiting:
            for task in waiting:
                self._tasks.append_constraint(task.id, f"User reply: {user_text}")
                self._tasks.transition(task.id, TaskState.READY, reason=None)
        else:
            self._plan_tasks(user_text, interaction.id)
        created = self._tasks.list()
        interaction.task_ids = [task.id for task in created if task.interaction_id == interaction.id]
        suggestion_note = self._maybe_suggest_domain(user_text)
        if suggestion_note:
            messages.append(suggestion_note)

        ready = self._tasks.next_ready()
        while ready is not None:
            result_messages = self._run_task(ready, interaction.id)
            messages.extend(result_messages)
            ready = self._tasks.next_ready()

        text = "\n".join(messages)
        self._sessions.append_assistant(interaction.id, text, task_ids=interaction.task_ids)
        return TurnResult(interaction_id=interaction.id, messages=messages, tasks=self._tasks.list())

    def _handle_structure_command(self, user_text: str) -> list[str] | None:
        """Explicit creation and suggestion confirmation are harness operations.

        The model may only suggest structure. It can never confirm its own
        proposal, so a bare model call cannot create persistent structure.
        """
        text = user_text.strip()
        lowered = text.lower()
        affirmative = lowered in {"yes", "yeah", "yep", "sure", "ok", "okay", "do it", "confirm", "go ahead"}
        negative = lowered in {"no", "nope", "not now", "don't", "do not", "cancel", "not yet"}
        pending = [item for item in self._domains.list_suggestions() if item.status == "pending"]
        if pending and (affirmative or negative):
            suggestion = sorted(pending, key=lambda item: item.ts)[-1]
            if affirmative:
                created = self._domains.confirm_suggestion(suggestion.id)
                return [f"Created {suggestion.draft.kind} '{created.name}' ({created.id})."]
            self._domains.decline_suggestion(suggestion.id)
            return ["Okay, I won't create that. I'll wait before suggesting it again."]

        for kind, pattern in (
            ("domain", r"(?:make|create|start)\s+(?:a\s+)?(?:new\s+)?domain(?:\s+(?:where|about|for|to talk about))?\s*[:\-]?\s*(.*)"),
            ("project", r"(?:make|create|start)\s+(?:a\s+)?(?:new\s+)?project(?:\s+(?:where|about|for|called|named))?\s*[:\-]?\s*(.*)"),
        ):
            match = re.search(pattern, lowered)
            if not match:
                continue
            name = text[match.start(1) :].strip().strip(".?!\"'")
            name = re.sub(r"^(?:where\s+)?(?:we(?:'ll| will)?\s+(?:talk|work)\s+(?:about|on)\s+)", "", name).strip()
            name = re.sub(r"^(?:about|for|on)\s+", "", name).strip()
            if not name:
                return [f"What should the {kind} be about?"]
            draft = StructureDraft(
                kind=kind,
                name=name,
                reason="explicit user command",
                evidence=f"user said: {text}",
            )
            if kind == "domain":
                created = self._domains.create_domain(draft, confirmed=True)
            else:
                created = self._domains.create_project(draft, confirmed=True)
            return [f"Created {kind} '{created.name}' ({created.id}). You can rename or archive it in the UI."]
        return None

    def _plan_tasks(self, user_text: str, interaction_id: str) -> None:
        trace_id = new_id("trc")
        prompt = [
            Message(role="system", content=TASK_PLAN_INSTRUCTIONS.strip()),
            Message(role="user", content=user_text),
        ]
        try:
            result = self._model.complete("interactive", prompt, schema=TASK_PLAN_SCHEMA)
        except ModelUnavailable as exc:
            trace = Trace(
                id=trace_id,
                ts=now_ts(),
                interaction_id=interaction_id,
                task_id="",
                role="planner",
                messages=[{"role": item.role, "content": item.content} for item in prompt],
                raw_output=None,
                validation={"ok": False, "error": str(exc)},
            )
            self._finish_trace(trace, interaction_id)
            plan = []
        else:
            try:
                plan = parse_task_plan(result.content)
                validation = (
                    {"ok": True, "tasks": len(plan)}
                    if plan
                    else {"ok": False, "error": "no usable task plan"}
                )
            except (ValueError, json.JSONDecodeError) as exc:
                plan = []
                validation = {"ok": False, "error": str(exc)}
            trace = Trace(
                id=trace_id,
                ts=now_ts(),
                interaction_id=interaction_id,
                task_id="",
                role="planner",
                messages=[{"role": item.role, "content": item.content} for item in prompt],
                raw_output=result.content,
                latency_ms=result.latency_ms,
                finish_reason=result.finish_reason,
                token_counts={
                    "prompt": result.prompt_tokens or 0,
                    "completion": result.completion_tokens or 0,
                },
                parsed={"tasks": len(plan)},
                validation=validation,
            )
            self._finish_trace(trace, interaction_id)
        if not plan:
            plan = [{"goal": user_text, "domain_name": None, "project_name": None, "depends_on": []}]
        created: list[Task] = []
        for spec in plan:
            depends: list[str] = []
            for idx in spec.get("depends_on") or []:
                if isinstance(idx, int) and 0 <= idx < len(created):
                    depends.append(created[idx].id)
            domain_id: str | None = None
            name = spec.get("domain_name")
            if name:
                if str(name).strip().lower() == SELF_DOMAIN_ID:
                    domain_id = SELF_DOMAIN_ID
                else:
                    found = self._domains.find_domain_by_name(str(name))
                    if found is not None:
                        domain_id = found.id
            project_id: str | None = None
            project_name = spec.get("project_name")
            if project_name:
                project = self._domains.find_project_by_name(str(project_name))
                if project is not None:
                    project_id = project.id
                    domain_id = project.domain_id
            created.append(
                self._tasks.create(
                    TaskSpec(
                        goal=str(spec.get("goal") or user_text),
                        domain_id=domain_id,
                        project_id=project_id,
                        depends_on=depends,
                        interaction_id=interaction_id,
                    )
                )
            )

    def _maybe_suggest_domain(self, user_text: str) -> str | None:
        count = int(self._settings.get("domain.suggestion.subject_words"))
        words = [part for part in user_text.lower().split() if part.isalpha()][:count]
        if not words:
            return None
        key = "-".join(words)
        hits = self._sessions.bump_subject(key)
        threshold = int(self._settings.get("domain.suggestion.threshold"))
        if hits < threshold:
            return None
        if self._domains.suggestion_on_cooldown(key):
            return None
        if self._domains.find_domain_by_name(key):
            return None
        if any(item.draft.name == key and item.status == "pending" for item in self._domains.list_suggestions()):
            return None
        self._domains.record_suggestion(
            StructureDraft(kind="domain", name=key, reason="repeated subject hits", evidence=f"hits={hits}")
        )
        return f"We've talked about this a few times, want me to create a domain for '{' '.join(words)}'?"

    def _run_task(self, task: Task, interaction_id: str) -> list[str]:
        self._tasks.transition(task.id, TaskState.RUNNING, reason=None)
        task = self._tasks.get(task.id)
        started = time.monotonic()
        limit_iter = int(self._settings.get("loop.max_iterations"))
        limit_s = float(self._settings.get("loop.time_limit_s"))
        invalid_budget = int(self._settings.get("loop.invalid_proposal_retries"))
        identical_limit = int(self._settings.get("loop.identical_retry_limit"))
        extra: list[Message] = []
        fingerprints: dict[str, dict[str, Any]] = {}
        delivered: list[str] = []
        iterations = 0
        invalid_used = 0
        while iterations < limit_iter:
            if time.monotonic() - started > limit_s:
                self._tasks.transition(task.id, TaskState.BLOCKED, reason=BlockReason.LIMIT)
                delivered.append("Stopped: time limit reached.")
                break
            task = self._tasks.get(task.id)
            trace_id = new_id("trc")
            try:
                package = self._context.build(task, self._sessions.current(), trace_id=trace_id)
            except ValueError as exc:
                self._tasks.transition(task.id, TaskState.FAILED, reason=None)
                delivered.append(str(exc))
                break
            prompt = list(package.messages) + extra
            try:
                model_result = self._model.complete("interactive", prompt, schema=PROPOSAL_SCHEMA)
            except ModelUnavailable as exc:
                self._tasks.transition(task.id, TaskState.BLOCKED, reason=BlockReason.MODEL_UNAVAILABLE)
                delivered.append(f"Inference unavailable: {exc}")
                break
            trace = Trace(
                id=trace_id,
                ts=now_ts(),
                interaction_id=interaction_id,
                task_id=task.id,
                role="interactive",
                messages=[{"role": m.role, "content": m.content} for m in prompt],
                included=[asdict(item) for item in package.included],
                excluded=[asdict(item) for item in package.excluded],
                raw_output=model_result.content,
                latency_ms=model_result.latency_ms,
                finish_reason=model_result.finish_reason,
                token_counts={
                    "prompt": model_result.prompt_tokens or package.total_tokens,
                    "completion": model_result.completion_tokens or 0,
                },
            )
            try:
                proposal = parse_proposal(model_result.content)
            except (ValueError, json.JSONDecodeError) as exc:
                invalid_used += 1
                trace.validation = {"ok": False, "error": str(exc)}
                trace.retries = invalid_used
                self._finish_trace(trace, interaction_id)
                extra.append(Message(role="assistant", content=model_result.content))
                extra.append(Message(role="user", content=f"Invalid proposal: {exc}. Return one valid JSON action."))
                if invalid_used > invalid_budget:
                    self._tasks.transition(task.id, TaskState.FAILED, reason=None)
                    delivered.append("Failed: invalid model proposals exhausted retries.")
                    break
                continue
            trace.parsed = {"kind": proposal.kind}
            machine = apply_action(task, proposal)
            if machine.error:
                invalid_used += 1
                trace.validation = {"ok": False, "error": machine.error}
                self._finish_trace(trace, interaction_id)
                extra.append(Message(role="assistant", content=model_result.content))
                extra.append(Message(role="user", content=f"Rejected: {machine.error}"))
                if invalid_used > invalid_budget:
                    self._tasks.transition(task.id, TaskState.FAILED, reason=None)
                    delivered.append(f"Failed: {machine.error}")
                    break
                continue
            if isinstance(proposal, ActAction):
                denied = self._identical_denied(proposal, fingerprints, identical_limit)
                if denied:
                    invalid_used += 1
                    trace.validation = {"ok": False, "error": denied}
                    self._finish_trace(trace, interaction_id)
                    extra.append(Message(role="user", content=denied))
                    if invalid_used > invalid_budget:
                        self._tasks.transition(task.id, TaskState.FAILED, reason=None)
                        delivered.append(denied)
                        break
                    continue
                execution = self._capabilities.execute(
                    ExecuteRequest(
                        task_id=task.id,
                        domain_id=task.domain_id,
                        capability=proposal.capability,
                        operation=proposal.operation,
                        arguments=proposal.arguments,
                        interaction_id=interaction_id,
                    )
                )
                self._record_fingerprint(proposal, execution, fingerprints)
                machine = apply_action(task, proposal, execution)
                if execution.status == ExecutionStatus.SUCCEEDED:
                    self._tasks.mark_succeeded_execution(task.id)
                extra.append(Message(role="assistant", content=model_result.content))
                extra.append(Message(role="user", content=self._envelope_text(execution)))
                if execution.status == ExecutionStatus.UNAVAILABLE:
                    extra.append(
                        Message(
                            role="user",
                            content="Capability unavailable. Explain the limitation. Do not claim it ran. Unrelated work may continue.",
                        )
                    )
                trace.validation = {"ok": True, "execution": execution.status}
                self._finish_trace(trace, interaction_id)
                self._events.append(
                    Event(
                        id=new_id("evt"),
                        ts=now_ts(),
                        actor="model",
                        operation="loop.act",
                        target=task.id,
                        result=execution.status,
                        trigger_ref=interaction_id,
                    )
                )
                iterations += 1
                continue
            trace.validation = {"ok": True, "action": proposal.kind}
            self._finish_trace(trace, interaction_id)
            if machine.deliver:
                self._tasks.mark_delivered(task.id, machine.deliver)
                delivered.append(machine.deliver)
            if machine.task_state != task.state:
                self._tasks.transition(task.id, machine.task_state, reason=machine.block_reason)
            if machine.complete or machine.task_state != TaskState.RUNNING:
                if machine.complete:
                    self._tasks.record_outcome(
                        task.id,
                        TaskOutcome(summary=machine.deliver or "", status=machine.task_state),
                    )
                break
            extra.append(Message(role="assistant", content=model_result.content))
            extra.append(Message(role="user", content="Continue with the next single action."))
            iterations += 1
        else:
            self._tasks.transition(task.id, TaskState.BLOCKED, reason=BlockReason.LIMIT)
            delivered.append("Stopped: iteration limit reached.")
        return delivered

    def _finish_trace(self, trace: Trace, interaction_id: str) -> None:
        self._traces.put(trace)
        validation = trace.validation or {}
        result = "ok" if validation.get("ok") else "invalid"
        if validation.get("execution"):
            result = str(validation["execution"])
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=now_ts(),
                actor="model",
                operation="model.call",
                target=trace.id,
                result=result,
                trigger_ref=interaction_id,
                refs=[trace.task_id],
                data={
                    "role": trace.role,
                    "latency_ms": trace.latency_ms,
                    "tokens": trace.token_counts,
                },
            )
        )

    def _fingerprint(self, proposal: ActAction) -> str:
        return json.dumps(
            {"capability": proposal.capability, "operation": proposal.operation, "arguments": proposal.arguments},
            sort_keys=True,
            ensure_ascii=False,
        )

    def _record_fingerprint(self, proposal: ActAction, execution: ExecutionResult, store: dict[str, dict[str, Any]]) -> None:
        key = self._fingerprint(proposal)
        item = store.setdefault(key, {"count": 0, "status": execution.status, "error_category": execution.error_category})
        item["count"] += 1
        item["status"] = execution.status
        item["error_category"] = execution.error_category

    def _identical_denied(self, proposal: ActAction, store: dict[str, dict[str, Any]], identical_limit: int) -> str | None:
        key = self._fingerprint(proposal)
        prior = store.get(key)
        if not prior:
            return None
        if prior["status"] == ExecutionStatus.SUCCEEDED:
            return None
        permission = PermissionLevel.READ
        try:
            manifest = self._capabilities.get(proposal.capability)
            for op in manifest.operations:
                if op.name == proposal.operation:
                    permission = op.permission
                    retryable = set(op.retryable_error_categories)
                    break
            else:
                retryable = set()
        except KeyError:
            retryable = set()
        if prior["status"] == ExecutionStatus.UNKNOWN:
            if permission in {PermissionLevel.WRITE, PermissionLevel.DESTRUCTIVE}:
                return "Rejected identical retry of an unknown write/destructive operation."
            return None
        if prior["error_category"] in retryable and prior["count"] <= identical_limit:
            return None
        return "Rejected identical repeat of a call that did not succeed."

    def _envelope_text(self, execution: ExecutionResult) -> str:
        payload = {
            "operation_id": execution.operation_id,
            "status": execution.status,
            "capability": execution.capability,
            "operation": execution.operation,
            "error_category": execution.error_category,
            "message": execution.message,
            "evidence": execution.evidence,
            "data": f"<data>{json.dumps(execution.data, ensure_ascii=False)}</data>",
        }
        text = "Execution result envelope: " + json.dumps(payload, ensure_ascii=False)
        if execution.status == ExecutionStatus.UNKNOWN:
            text += " The outcome is uncertain. Do not repeat a write or destructive call; reconcile or ask the user."
        return text

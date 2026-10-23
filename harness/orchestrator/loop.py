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
from harness.contracts.model import Message, ModelAdapter, ModelResult, ModelUnavailable
from harness.contracts.session import SessionManager
from harness.contracts.settings import SettingsService
from harness.contracts.tasks import Task, TaskManager, TaskOutcome, TaskSpec
from harness.contracts.traces import Trace, TraceStore
from harness.orchestrator.machine import apply_action
from harness.orchestrator.parse import PROPOSAL_SCHEMA, TASK_PLAN_SCHEMA, parse_proposal, parse_task_plan
from harness.orchestrator.timeline import TurnTimeline
from harness.prompts import TASK_PLAN_INSTRUCTIONS
from harness.storage.atomic import new_id, now_ts
from harness.text import estimate_tokens, strip_reasoning


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
        timeline = TurnTimeline()
        interaction_started = time.monotonic()
        interaction = self._sessions.record_interaction(user_text)
        timeline.record("session", interaction_started, {"interaction_id": interaction.id})
        messages: list[str] = []
        structure_started = time.monotonic()
        structure = self._handle_structure_command(user_text)
        timeline.record("structure_check", structure_started, {"handled": structure is not None})
        if structure is not None:
            messages.extend(structure)
            text = "\n".join(messages)
            self._sessions.append_assistant(interaction.id, text, task_ids=[])
            timeline.stage("response", duration_ms=0, detail={"kind": "structure", "text": text})
            result = TurnResult(interaction_id=interaction.id, messages=messages, tasks=self._tasks.list())
            self._write_turn_trace(interaction.id, timeline, result)
            return result

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
            self._plan_tasks(user_text, interaction.id, timeline)
        created = self._tasks.list()
        interaction.task_ids = [task.id for task in created if task.interaction_id == interaction.id]
        suggestion_started = time.monotonic()
        suggestion_note = self._maybe_suggest_domain(user_text)
        timeline.record("domain_suggestion", suggestion_started, {"suggested": bool(suggestion_note)})
        if suggestion_note:
            messages.append(suggestion_note)

        ready = self._tasks.next_ready()
        while ready is not None:
            result_messages = self._run_task(ready, interaction.id, timeline)
            messages.extend(result_messages)
            ready = self._tasks.next_ready()

        text = "\n".join(messages)
        response_started = time.monotonic()
        self._sessions.append_assistant(interaction.id, text, task_ids=interaction.task_ids)
        timeline.record("turn.response", response_started, {"messages": messages})
        result = TurnResult(interaction_id=interaction.id, messages=messages, tasks=self._tasks.list())
        self._write_turn_trace(interaction.id, timeline, result)
        return result

    def _write_turn_trace(self, interaction_id: str, timeline: TurnTimeline, result: TurnResult) -> None:
        call_stages = [item for item in timeline.stages if item["stage"] == "model_call"]
        prompt_tokens = sum(int((item.get("detail") or {}).get("prompt_tokens") or 0) for item in call_stages)
        completion_tokens = sum(int((item.get("detail") or {}).get("completion_tokens") or 0) for item in call_stages)
        visible_tokens = sum(int((item.get("detail") or {}).get("visible_output_tokens") or 0) for item in call_stages)
        reasoning_tokens = sum(int((item.get("detail") or {}).get("reasoning_tokens") or 0) for item in call_stages)
        validation: dict[str, Any] = {
            "ok": result.error is None,
            "messages": len(result.messages),
            "tasks": len(result.tasks),
        }
        if result.error:
            validation["error"] = result.error
        trace = Trace(
            id=new_id("trc"),
            ts=now_ts(),
            interaction_id=interaction_id,
            task_id="",
            role="turn",
            messages=[],
            duration_ms=timeline.total_ms(),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            visible_output_tokens=visible_tokens,
            reasoning_tokens=reasoning_tokens,
            thinking_produced=reasoning_tokens > 0,
            model_called=False,
            timeline=timeline.snapshot(),
            token_counts={
                "prompt": prompt_tokens,
                "completion": completion_tokens,
                "visible_output": visible_tokens,
                "reasoning": reasoning_tokens,
            },
            validation=validation,
        )
        self._traces.put(trace)

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

    def _plan_tasks(self, user_text: str, interaction_id: str, timeline: TurnTimeline) -> None:
        trace_id = new_id("trc")
        prompt_started = time.monotonic()
        prompt = [
            Message(role="system", content=TASK_PLAN_INSTRUCTIONS.strip()),
            Message(role="user", content=user_text),
        ]
        timeline.record("plan.prompt_build", prompt_started, {"messages": len(prompt)})
        if self._heuristic_single_task(user_text):
            heuristic_started = time.monotonic()
            timeline.record(
                "plan.heuristic",
                heuristic_started,
                {
                    "max_chars": self._settings.get("tasks.heuristic_max_chars"),
                    "max_words": self._settings.get("tasks.heuristic_max_words"),
                    "markers": self._settings.get("tasks.heuristic_split_markers"),
                },
            )
            trace = Trace(
                id=trace_id,
                ts=now_ts(),
                interaction_id=interaction_id,
                task_id="",
                role="planner",
                messages=[{"role": item.role, "content": item.content} for item in prompt],
                raw_output=None,
                duration_ms=int((time.monotonic() - prompt_started) * 1000),
                model_called=False,
                validation={"ok": True, "source": "heuristic", "tasks": 1},
            )
            self._finish_trace(trace, interaction_id, timeline)
            create_started = time.monotonic()
            created = self._create_plan_tasks(
                [{"goal": user_text, "domain_name": None, "project_name": None, "depends_on": []}],
                user_text,
                interaction_id,
            )
            timeline.record("plan.task_create", create_started, {"tasks": [task.id for task in created]})
            self._events.append(
                Event(
                    id=new_id("evt"),
                    ts=now_ts(),
                    actor="harness",
                    operation="plan.heuristic",
                    target=trace.id,
                    result="ok",
                    trigger_ref=interaction_id,
                    data={"tasks": len(created)},
                )
            )
            return
        call_started = time.monotonic()
        try:
            result = self._model.complete("interactive", prompt, schema=TASK_PLAN_SCHEMA)
        except ModelUnavailable as exc:
            timeline.record("plan.model_call", call_started, {"error": str(exc)})
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
            self._finish_trace(trace, interaction_id, timeline)
            plan = []
        else:
            metrics = self._call_metrics(result)
            timeline.record(
                "plan.model_call",
                call_started,
                {"trace_id": trace_id, "duration_ms": result.latency_ms, **metrics},
            )
            parse_started = time.monotonic()
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
            timeline.record(
                "plan.parse_validation",
                parse_started,
                {"ok": validation.get("ok"), "tasks": len(plan), **self._stage_metrics(metrics)},
            )
            trace = self._model_trace(
                trace_id=trace_id,
                interaction_id=interaction_id,
                task_id="",
                role="planner",
                prompt=prompt,
                result=result,
                metrics=metrics,
                parsed={"tasks": len(plan)},
                validation=validation,
            )
            self._finish_trace(trace, interaction_id, timeline)
        if not plan:
            plan = [{"goal": user_text, "domain_name": None, "project_name": None, "depends_on": []}]
        create_started = time.monotonic()
        created = self._create_plan_tasks(plan, user_text, interaction_id)
        timeline.record("plan.task_create", create_started, {"tasks": [task.id for task in created]})

    def _create_plan_tasks(self, plan: list[dict[str, Any]], user_text: str, interaction_id: str) -> list[Task]:
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
        return created

    def _heuristic_single_task(self, user_text: str) -> bool:
        if str(self._settings.get("tasks.planning_mode")) != "heuristic":
            return False
        text = user_text.strip()
        if not text:
            return False
        if len(text) > int(self._settings.get("tasks.heuristic_max_chars")):
            return False
        if len(text.split()) > int(self._settings.get("tasks.heuristic_max_words")):
            return False
        raw = str(self._settings.get("tasks.heuristic_split_markers") or "")
        lowered = text.lower()
        for marker in raw.split("|"):
            marker = marker.strip().lower()
            if not marker:
                continue
            if re.search(r"(?<!\w)" + re.escape(marker) + r"(?!\w)", lowered):
                return False
        return True

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

    def _run_task(self, task: Task, interaction_id: str, timeline: TurnTimeline) -> list[str]:
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
        stop_after_failure = bool(self._settings.get("loop.stop_after_nonretryable_failure"))
        stopped_failure: ExecutionResult | None = None
        stop_rejections = 0
        while iterations < limit_iter:
            if time.monotonic() - started > limit_s:
                self._tasks.transition(task.id, TaskState.BLOCKED, reason=BlockReason.LIMIT)
                delivered.append("Stopped: time limit reached.")
                break
            task = self._tasks.get(task.id)
            trace_id = new_id("trc")
            build_started = time.monotonic()
            try:
                package = self._context.build(task, self._sessions.current(), trace_id=trace_id)
            except ValueError as exc:
                self._tasks.transition(task.id, TaskState.FAILED, reason=None)
                delivered.append(str(exc))
                break
            timeline.record(
                "prompt_build",
                build_started,
                {"task_id": task.id, "tokens": package.total_tokens, "included": len(package.included), "excluded": len(package.excluded)},
            )
            prompt = list(package.messages) + extra
            call_started = time.monotonic()
            try:
                model_result = self._model.complete("interactive", prompt, schema=PROPOSAL_SCHEMA)
            except ModelUnavailable as exc:
                timeline.record("model_call", call_started, {"task_id": task.id, "error": str(exc)})
                self._tasks.transition(task.id, TaskState.BLOCKED, reason=BlockReason.MODEL_UNAVAILABLE)
                delivered.append(f"Inference unavailable: {exc}")
                break
            metrics = self._call_metrics(model_result)
            timeline.record(
                "model_call",
                call_started,
                {"task_id": task.id, "trace_id": trace_id, "duration_ms": model_result.latency_ms, **metrics},
            )
            trace = self._model_trace(
                trace_id=trace_id,
                interaction_id=interaction_id,
                task_id=task.id,
                role="interactive",
                prompt=prompt,
                result=model_result,
                metrics=metrics,
                included=[asdict(item) for item in package.included],
                excluded=[asdict(item) for item in package.excluded],
                prompt_fallback=package.total_tokens,
            )
            parse_started = time.monotonic()
            try:
                proposal = parse_proposal(model_result.content)
            except (ValueError, json.JSONDecodeError) as exc:
                invalid_used += 1
                trace.validation = {"ok": False, "error": str(exc)}
                trace.retries = invalid_used
                timeline.record("parse_validation", parse_started, {"ok": False, "error": str(exc)})
                self._finish_trace(trace, interaction_id, timeline)
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
                timeline.record("parse_validation", parse_started, {"ok": False, "error": machine.error})
                self._finish_trace(trace, interaction_id, timeline)
                extra.append(Message(role="assistant", content=model_result.content))
                extra.append(Message(role="user", content=f"Rejected: {machine.error}"))
                if invalid_used > invalid_budget:
                    self._tasks.transition(task.id, TaskState.FAILED, reason=None)
                    delivered.append(f"Failed: {machine.error}")
                    break
                continue
            if isinstance(proposal, ActAction) and stopped_failure is not None:
                stop_rejections += 1
                reason = (
                    f"Execution failed with non-retryable error_category "
                    f"'{stopped_failure.error_category or stopped_failure.status}': "
                    f"{stopped_failure.message}. Do not call another operation. Tell the user "
                    "exactly what failed and do not claim success."
                )
                trace.validation = {"ok": False, "error": reason, "blocked": "nonretryable_failure"}
                timeline.record("parse_validation", parse_started, {"ok": False, "error": reason})
                self._finish_trace(trace, interaction_id, timeline)
                extra.append(Message(role="assistant", content=model_result.content))
                extra.append(Message(role="user", content=reason))
                if stop_rejections > 1:
                    text = self._failure_response(stopped_failure)
                    self._tasks.mark_delivered(task.id, text)
                    delivered.append(text)
                    self._tasks.transition(task.id, TaskState.COMPLETED)
                    self._tasks.record_outcome(
                        task.id, TaskOutcome(summary=text, status=TaskState.COMPLETED)
                    )
                    timeline.stage(
                        "response",
                        duration_ms=0,
                        detail={"kind": "harness_failure", "task_id": task.id, "text": text},
                    )
                    break
                continue
            if isinstance(proposal, ActAction):
                denied = self._identical_denied(proposal, fingerprints, identical_limit)
                if denied:
                    invalid_used += 1
                    trace.validation = {"ok": False, "error": denied}
                    timeline.record("parse_validation", parse_started, {"ok": False, "error": denied})
                    self._finish_trace(trace, interaction_id, timeline)
                    extra.append(Message(role="user", content=denied))
                    if invalid_used > invalid_budget:
                        self._tasks.transition(task.id, TaskState.FAILED, reason=None)
                        delivered.append(denied)
                        break
                    continue
                timeline.record("parse_validation", parse_started, {"ok": True, "kind": proposal.kind, "action": "act"})
                execution_started = time.monotonic()
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
                envelope = self._envelope_dict(execution)
                timeline.record("execution", execution_started, envelope)
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
                if (
                    stop_after_failure
                    and execution.status not in {ExecutionStatus.SUCCEEDED, ExecutionStatus.UNKNOWN}
                    and not self._execution_retryable(proposal, execution)
                ):
                    stopped_failure = execution
                    extra.append(
                        Message(
                            role="user",
                            content=(
                                f"Execution failed with non-retryable error_category "
                                f"'{execution.error_category or execution.status}'. Do not call "
                                "another operation. Tell the user exactly what failed and do not "
                                "claim success."
                            ),
                        )
                    )
                trace.validation = {"ok": True, "execution": execution.status, "envelope": envelope}
                self._finish_trace(trace, interaction_id, timeline)
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
            timeline.record("parse_validation", parse_started, {"ok": True, "kind": proposal.kind})
            trace.validation = {"ok": True, "action": proposal.kind}
            self._finish_trace(trace, interaction_id, timeline)
            if machine.deliver:
                self._tasks.mark_delivered(task.id, machine.deliver)
                delivered.append(machine.deliver)
                timeline.stage(
                    "response",
                    duration_ms=0,
                    detail={"kind": proposal.kind, "task_id": task.id, "text": machine.deliver},
                )
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

    def _call_metrics(self, result: ModelResult) -> dict[str, Any]:
        chars_per_token = int(self._settings.get("context.chars_per_token"))
        slack = int(self._settings.get("models.reasoning_token_slack"))
        visible = estimate_tokens(strip_reasoning(result.content), chars_per_token=chars_per_token)
        completion = int(result.completion_tokens or 0)
        reasoning_text = result.reasoning_content or ""
        reasoning = estimate_tokens(reasoning_text, chars_per_token=chars_per_token) if reasoning_text else 0
        budget = self._settings.get("models.interactive.thinking_budget_tokens")
        thinking_requested = bool(self._settings.get("models.interactive.enable_thinking")) and (
            budget is None or int(budget) > 0
        )
        if not reasoning and thinking_requested and completion > visible + slack:
            # The server may hide reasoning from the response body but still
            # bill it in completion_tokens.
            reasoning = completion - visible
        return {
            "prompt_tokens": int(result.prompt_tokens or 0),
            "completion_tokens": completion,
            "visible_output_tokens": visible,
            "reasoning_tokens": reasoning,
            "thinking_produced": bool(reasoning_text) or reasoning > 0,
            "time_to_first_token_ms": result.time_to_first_token_ms,
        }

    @staticmethod
    def _stage_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
        return {
            "prompt_tokens": metrics.get("prompt_tokens"),
            "completion_tokens": metrics.get("completion_tokens"),
            "visible_output_tokens": metrics.get("visible_output_tokens"),
            "reasoning_tokens": metrics.get("reasoning_tokens"),
        }

    def _model_trace(
        self,
        *,
        trace_id: str,
        interaction_id: str,
        task_id: str,
        role: str,
        prompt: list[Message],
        result: ModelResult,
        metrics: dict[str, Any],
        prompt_fallback: int = 0,
        included: list[dict[str, Any]] | None = None,
        excluded: list[dict[str, Any]] | None = None,
        parsed: dict[str, Any] | None = None,
        validation: dict[str, Any] | None = None,
    ) -> Trace:
        prompt_tokens = int(result.prompt_tokens or prompt_fallback or 0)
        completion_tokens = int(metrics.get("completion_tokens") or 0)
        visible_tokens = int(metrics.get("visible_output_tokens") or 0)
        reasoning_tokens = int(metrics.get("reasoning_tokens") or 0)
        return Trace(
            id=trace_id,
            ts=now_ts(),
            interaction_id=interaction_id,
            task_id=task_id,
            role=role,
            messages=[{"role": item.role, "content": item.content} for item in prompt],
            included=included or [],
            excluded=excluded or [],
            raw_output=result.content,
            latency_ms=result.latency_ms,
            duration_ms=result.latency_ms,
            time_to_first_token_ms=result.time_to_first_token_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            visible_output_tokens=visible_tokens,
            reasoning_tokens=reasoning_tokens,
            thinking_produced=bool(metrics.get("thinking_produced")),
            finish_reason=result.finish_reason,
            token_counts={
                "prompt": prompt_tokens,
                "completion": completion_tokens,
                "visible_output": visible_tokens,
                "reasoning": reasoning_tokens,
            },
            parsed=parsed,
            validation=validation,
        )

    def _finish_trace(self, trace: Trace, interaction_id: str, timeline: TurnTimeline | None = None) -> None:
        if timeline is not None:
            trace.timeline = timeline.snapshot()
        self._traces.put(trace)
        if not trace.model_called:
            return
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
                    "duration_ms": trace.duration_ms,
                    "time_to_first_token_ms": trace.time_to_first_token_ms,
                    "tokens": trace.token_counts,
                    "thinking_produced": trace.thinking_produced,
                },
            )
        )

    def _fingerprint(self, proposal: ActAction) -> str:
        return json.dumps(
            {"capability": proposal.capability, "operation": proposal.operation, "arguments": proposal.arguments},
            sort_keys=True,
            ensure_ascii=False,
        )

    def _execution_retryable(self, proposal: ActAction, execution: ExecutionResult) -> bool:
        try:
            manifest = self._capabilities.get(proposal.capability)
        except KeyError:
            return False
        for op in manifest.operations:
            if op.name == proposal.operation:
                return execution.error_category in set(op.retryable_error_categories)
        return False

    def _failure_response(self, execution: ExecutionResult) -> str:
        category = execution.error_category or execution.status
        return (
            f"I couldn't complete {execution.capability}.{execution.operation}: "
            f"{execution.message} ({category}). I have not reported it as done."
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

    def _envelope_dict(self, execution: ExecutionResult) -> dict[str, Any]:
        return {
            "operation_id": execution.operation_id,
            "status": execution.status,
            "capability": execution.capability,
            "operation": execution.operation,
            "error_category": execution.error_category,
            "message": execution.message,
            "evidence": execution.evidence,
            "data": execution.data,
        }

    def _envelope_text(self, execution: ExecutionResult) -> str:
        payload = self._envelope_dict(execution)
        payload["data"] = f"<data>{json.dumps(execution.data, ensure_ascii=False)}</data>"
        text = "Execution result envelope: " + json.dumps(payload, ensure_ascii=False)
        if execution.status == ExecutionStatus.UNKNOWN:
            text += " The outcome is uncertain. Do not repeat a write or destructive call; reconcile or ask the user."
        return text

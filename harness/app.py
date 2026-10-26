from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from harness.capabilities.executor import FileCapabilityExecutor
from harness.consolidation.service import FileConsolidationService
from harness.context.builder import DefaultContextBuilder
from harness.contracts.capabilities import ExecuteRequest
from harness.contracts.control import ControlPlane
from harness.contracts.decisions import DecisionResolver, DecisionStore
from harness.contracts.domains import StructureDraft
from harness.control.decisions import FileDecisionStore
from harness.control.plane import DefaultControlPlane
from harness.control.resolver import DefaultDecisionResolver
from harness.domains.inference import DefaultDomainInference
from harness.domains.registry import FileDomainRegistry
from harness.events.log import FileEventLog
from harness.history.service import FileHistoryService
from harness.maintenance.reset import FileResetService
from harness.memory.capture import ExecutionOutcomeCapture
from harness.memory.classifier import KeywordMemoryClassifier
from harness.memory.file_manager import FileMemoryManager
from harness.memory.inmemory import InMemoryMemoryManager
from harness.memory.semantic import LexicalSemanticScorer
from harness.model.openai_http import OpenAICompatAdapter
from harness.model.scripted import ScriptedAdapter
from harness.orchestrator.loop import DefaultInteractionLoop
from harness.profile.provider import FileSelfProfile
from harness.scheduler.service import WorkScheduler
from harness.session.manager import FileSessionManager
from harness.session.turns import DefaultTurnService
from harness.settings.service import FileSettingsService
from harness.sources import CompositeSourceResolver
from harness.tasks.manager import FileTaskManager
from harness.text import match_by_name, name_overlap, normalize_name
from harness.traces.store import FileTraceStore


@dataclass
class Harness:
    data_dir: Path
    settings: FileSettingsService
    events: FileEventLog
    traces: FileTraceStore
    sessions: FileSessionManager
    domains: FileDomainRegistry
    tasks: FileTaskManager
    decisions: DecisionStore
    decision_resolver: DecisionResolver
    control: ControlPlane
    profile: FileSelfProfile
    capabilities: FileCapabilityExecutor
    memory: FileMemoryManager | InMemoryMemoryManager
    history: FileHistoryService
    context: DefaultContextBuilder
    model: object
    consolidation: FileConsolidationService
    loop: DefaultInteractionLoop
    chat: DefaultTurnService
    scheduler: WorkScheduler
    reset: FileResetService

    def on_start(self) -> None:
        self.tasks.on_process_start()
        self.control.on_start()
        # Deterministic recovery runs even when the heartbeat is disabled:
        # interrupted and unknown-outcome work must never look runnable.
        self.scheduler.reconcile()


def build_harness(
    data_dir: str | Path,
    *,
    env: dict[str, str] | None = None,
    model: object | None = None,
) -> Harness:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    environ = env if env is not None else dict(os.environ)
    settings = FileSettingsService(root / "settings.yaml", env=environ)
    events = FileEventLog(root / "events", settings)
    settings.attach_events(events)
    traces = FileTraceStore(root / "traces", settings)
    sessions = FileSessionManager(root / "sessions", settings, events)
    domains = FileDomainRegistry(root / "registry", settings, events)
    tasks = FileTaskManager(root / "tasks", settings, events)
    decisions = FileDecisionStore(root / "decisions", settings, events)
    profile = FileSelfProfile(root / "persona", settings, events)
    capabilities = FileCapabilityExecutor(root / "capabilities", settings, events, domains)
    decision_resolver = DefaultDecisionResolver()
    control = DefaultControlPlane(
        settings=settings,
        sessions=sessions,
        tasks=tasks,
        domains=domains,
        capabilities=capabilities,
        decisions=decisions,
        resolver=decision_resolver,
        events=events,
    )
    domains.attach_suggestion_listener(decisions.sync_suggestion)

    def same_name(left: str, right: str) -> bool:
        if normalize_name(left) == normalize_name(right):
            return True
        threshold = float(settings.get("domain.match_overlap"))
        return name_overlap(left, right) >= threshold

    def pending_suggestion(name: str):
        for item in domains.list_suggestions():
            if (
                item.status == "pending"
                and same_name(item.draft.name, name)
            ):
                return item
        return None

    def available_domains() -> list[str]:
        return [item.name for item in domains.list_domains()]

    def resolve_domain_failure(name: str, candidates: list[str]) -> dict:
        if candidates:
            return {
                "status": "failed",
                "data": {"candidates": candidates},
                "message": f"ambiguous domain '{name}'; candidates: {', '.join(candidates)}",
                "error_category": "ambiguous",
            }
        available = available_domains()
        return {
            "status": "failed",
            "data": {"available": available},
            "message": f"no existing domain named '{name}'; available: {', '.join(available)}",
            "error_category": "not_found",
        }

    def structure_hook(operation: str, arguments: dict, req: ExecuteRequest) -> dict:
        if operation == "enter_domain":
            name = str(arguments.get("name") or "").strip()
            domain, candidates = match_by_name(domains.list_domains(), name)
            if domain is None:
                return resolve_domain_failure(name, candidates)
            sessions.set_active_domain(domain.id)
            if req.task_id:
                try:
                    tasks.assign_domain(req.task_id, domain.id)
                except KeyError:
                    # The task may already be gone; the session switch still
                    # applies to the next turn.
                    pass
            return {
                "status": "succeeded",
                "data": {"domain_id": domain.id, "name": domain.name},
                "message": f"entered domain '{domain.name}'",
            }
        if operation == "leave_domain":
            sessions.set_active_domain(None)
            if req.task_id:
                try:
                    tasks.assign_domain(req.task_id, None)
                except KeyError:
                    pass
            return {"status": "succeeded", "data": {}, "message": "left the active domain"}
        if operation == "suggest_domain":
            name = str(arguments.get("name") or "").strip()
            existing, _ = match_by_name(domains.list_domains(), name)
            if existing is not None:
                return {
                    "status": "failed",
                    "data": {"existing_domain": {"id": existing.id, "name": existing.name}},
                    "message": f"domain '{existing.name}' already exists",
                    "error_category": "duplicate",
                }
            if domains.suggestion_on_cooldown(name):
                return {
                    "status": "failed",
                    "data": {"cooldown": True},
                    "message": f"domain '{name}' is on suggestion cooldown",
                    "error_category": "cooldown",
                }
            pending = pending_suggestion(name)
            if pending is not None:
                return {
                    "status": "failed",
                    "data": {"duplicate": True, "suggestion_id": pending.id},
                    "message": f"a pending suggestion for '{pending.draft.name}' already exists",
                    "error_category": "duplicate",
                }
            draft = StructureDraft(
                name=name,
                reason=str(arguments.get("reason") or "assistant suggestion"),
                evidence="model proposal",
            )
            suggestion = domains.record_suggestion(draft)
            return {"status": "succeeded", "data": {"suggestion_id": suggestion.id}, "message": "suggestion recorded"}
        return {
            "status": "failed",
            "data": {},
            "message": "structure creation requires explicit user confirmation",
            "error_category": "unconfirmed",
        }

    capabilities.attach_structure(structure_hook)
    sources = CompositeSourceResolver(sessions, events, capabilities, settings)

    def scope_exists(scope: str) -> bool:
        if scope in {"global", "self"}:
            return True
        if scope.startswith("domain:"):
            try:
                domains.get_domain(scope.split(":", 1)[1])
                return True
            except KeyError:
                return False
        return False

    impl = str(settings.get("implementations.memory"))
    classifier_name = str(settings.get("implementations.memory_classifier"))
    classifier = KeywordMemoryClassifier(settings)
    if classifier_name != "keyword":
        classifier = KeywordMemoryClassifier(settings)
    retriever = str(settings.get("implementations.retriever"))
    semantic = LexicalSemanticScorer(settings) if retriever == "hybrid" else None
    if impl == "inmemory":
        memory: FileMemoryManager | InMemoryMemoryManager = InMemoryMemoryManager(
            settings, events, sources, scope_exists, classifier, semantic
        )
    else:
        memory = FileMemoryManager(
            root / "memory", settings, events, sources, scope_exists, classifier, semantic
        )
    capabilities.attach_memory(memory)
    if model is None:
        adapter_name = str(settings.get("implementations.model"))
        if adapter_name == "scripted":
            model = ScriptedAdapter()
        else:
            model = OpenAICompatAdapter(settings)
    inference = DefaultDomainInference(settings, domains, tasks, memory, sessions)
    history = FileHistoryService(root / "history", settings, sessions, model, events)
    context = DefaultContextBuilder(
        settings,
        memory,
        profile,
        domains,
        capabilities,
        sessions,
        history,
        decisions=decisions,
        inference=inference,
    )
    outcomes = ExecutionOutcomeCapture(settings=settings, events=events, memory=memory)
    loop = DefaultInteractionLoop(
        settings=settings,
        model=model,
        sessions=sessions,
        tasks=tasks,
        domains=domains,
        context=context,
        capabilities=capabilities,
        traces=traces,
        events=events,
        history=history,
        outcomes=outcomes,
        control=control,
        inference=inference,
    )
    consolidation = FileConsolidationService(
        root / "consolidation",
        settings,
        model,
        memory,
        sessions,
        events,
        profile,
        domains,
    )
    chat = DefaultTurnService(
        loop=loop,
        traces=traces,
        memory=memory,
        settings=settings,
    )
    scheduler = WorkScheduler(
        settings=settings,
        tasks=tasks,
        decisions=decisions,
        capabilities=capabilities,
        events=events,
        consolidation=consolidation,
        runner=lambda limit: chat.run_ready(limit=limit),
    )
    reset = FileResetService(
        root,
        settings=settings,
        sessions=sessions,
        profile=profile,
        domains=domains,
        memory=memory,
        events=events,
        # The scheduler is quiesced first: a tick holds this gate for its
        # whole duration (its runner also takes the turn lock), so a reset
        # can never interleave with background work.
        gates=[scheduler.exclusive, chat.exclusive, history.exclusive, consolidation.exclusive],
    )
    harness = Harness(
        data_dir=root,
        settings=settings,
        events=events,
        traces=traces,
        sessions=sessions,
        domains=domains,
        tasks=tasks,
        decisions=decisions,
        decision_resolver=decision_resolver,
        control=control,
        profile=profile,
        capabilities=capabilities,
        memory=memory,
        history=history,
        context=context,
        model=model,
        consolidation=consolidation,
        loop=loop,
        chat=chat,
        scheduler=scheduler,
        reset=reset,
    )
    harness.on_start()
    return harness

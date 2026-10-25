from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from harness.capabilities.executor import FileCapabilityExecutor
from harness.consolidation.service import FileConsolidationService
from harness.constants import SELF_DOMAIN_ID
from harness.context.builder import DefaultContextBuilder
from harness.contracts.domains import StructureDraft
from harness.domains.registry import FileDomainRegistry
from harness.events.log import FileEventLog
from harness.history.service import FileHistoryService
from harness.maintenance.reset import FileResetService
from harness.memory.classifier import KeywordMemoryClassifier
from harness.memory.file_manager import FileMemoryManager
from harness.memory.inmemory import InMemoryMemoryManager
from harness.model.openai_http import OpenAICompatAdapter
from harness.model.scripted import ScriptedAdapter
from harness.orchestrator.loop import DefaultInteractionLoop
from harness.profile.provider import FileSelfProfile
from harness.session.manager import FileSessionManager
from harness.session.turns import DefaultTurnService
from harness.settings.service import FileSettingsService
from harness.sources import CompositeSourceResolver
from harness.tasks.manager import FileTaskManager
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
    profile: FileSelfProfile
    capabilities: FileCapabilityExecutor
    memory: FileMemoryManager | InMemoryMemoryManager
    history: FileHistoryService
    context: DefaultContextBuilder
    model: object
    consolidation: FileConsolidationService
    loop: DefaultInteractionLoop
    chat: DefaultTurnService
    reset: FileResetService

    def on_start(self) -> None:
        self.tasks.on_process_start()


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
    profile = FileSelfProfile(root / "persona", settings, events)
    capabilities = FileCapabilityExecutor(root / "capabilities", settings, events, domains)

    def structure_hook(operation: str, arguments: dict) -> dict:
        if operation == "suggest_domain":
            draft = StructureDraft(
                kind="domain",
                name=str(arguments["name"]),
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
        if scope.startswith("project:"):
            try:
                domains.get_project(scope.split(":", 1)[1])
                return True
            except KeyError:
                return False
        return False

    impl = str(settings.get("implementations.memory"))
    classifier_name = str(settings.get("implementations.memory_classifier"))
    classifier = KeywordMemoryClassifier(settings)
    if classifier_name != "keyword":
        classifier = KeywordMemoryClassifier(settings)
    if impl == "inmemory":
        memory: FileMemoryManager | InMemoryMemoryManager = InMemoryMemoryManager(
            settings, events, sources, scope_exists, classifier
        )
    else:
        memory = FileMemoryManager(root / "memory", settings, events, sources, scope_exists, classifier)
    capabilities.attach_memory(memory)
    if model is None:
        adapter_name = str(settings.get("implementations.model"))
        if adapter_name == "scripted":
            model = ScriptedAdapter()
        else:
            model = OpenAICompatAdapter(settings)
    history = FileHistoryService(root / "history", settings, sessions, model, events)
    context = DefaultContextBuilder(settings, memory, profile, domains, capabilities, sessions, history)
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
    reset = FileResetService(
        root,
        settings=settings,
        sessions=sessions,
        profile=profile,
        domains=domains,
        memory=memory,
        events=events,
        gates=[chat.exclusive, history.exclusive, consolidation.exclusive],
    )
    harness = Harness(
        data_dir=root,
        settings=settings,
        events=events,
        traces=traces,
        sessions=sessions,
        domains=domains,
        tasks=tasks,
        profile=profile,
        capabilities=capabilities,
        memory=memory,
        history=history,
        context=context,
        model=model,
        consolidation=consolidation,
        loop=loop,
        chat=chat,
        reset=reset,
    )
    harness.on_start()
    return harness

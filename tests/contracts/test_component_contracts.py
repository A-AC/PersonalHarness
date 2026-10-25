from __future__ import annotations

import ast
import inspect
from pathlib import Path

from harness.app import Harness
from harness.contracts import (
    CapabilityExecutor,
    ConsolidationService,
    ContextBuilder,
    DomainRegistry,
    EventLog,
    InteractionLoop,
    HistoryService,
    MemoryManager,
    SelfProfile,
    SessionManager,
    SettingsService,
    TaskManager,
    TraceStore,
    ResetService,
)


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "harness"
SHARED = {"contracts", "constants", "storage", "text", "timefmt", "prompts"}


def harness_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("harness"):
            found.append(node.module)
        elif isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names if alias.name.startswith("harness"))
    return found


def test_contracts_import_only_contracts() -> None:
    for path in sorted((HARNESS / "contracts").glob("*.py")):
        for module in harness_imports(path):
            assert module == "harness" or module.startswith("harness.contracts"), f"{path.name} imports {module}"


def test_components_do_not_import_each_other_concretely() -> None:
    allowed_app_imports = {
        HARNESS / "app.py",
        HARNESS / "measure.py",
        HARNESS / "eval.py",
        HARNESS / "__main__.py",
        HARNESS / "ui" / "web.py",
    }
    for path in sorted(HARNESS.rglob("*.py")):
        relative = path.relative_to(HARNESS)
        if relative.parts[0] == "contracts" or path in allowed_app_imports:
            continue
        component = relative.parts[0] if len(relative.parts) > 1 else None
        for module in harness_imports(path):
            target = module.split(".")[1] if "." in module else ""
            if module == "harness":
                continue
            assert target in SHARED or target == component or target == "app", f"{relative} imports {module}"


def test_composition_root_satisfies_protocols(harness: Harness) -> None:
    bindings = [
        (SettingsService, harness.settings),
        (MemoryManager, harness.memory),
        (DomainRegistry, harness.domains),
        (TaskManager, harness.tasks),
        (SessionManager, harness.sessions),
        (HistoryService, harness.history),
        (EventLog, harness.events),
        (TraceStore, harness.traces),
        (SelfProfile, harness.profile),
        (CapabilityExecutor, harness.capabilities),
        (ContextBuilder, harness.context),
        (InteractionLoop, harness.loop),
        (ConsolidationService, harness.consolidation),
        (ResetService, harness.reset),
    ]
    for protocol, instance in bindings:
        for name, member in inspect.getmembers(protocol):
            if name.startswith("_"):
                continue
            if inspect.isfunction(member):
                assert callable(getattr(instance, name, None)), f"{type(instance).__name__} missing {name}"

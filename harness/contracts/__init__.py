"""Replaceable manager contracts. Types and signatures only."""

from harness.contracts.capabilities import CapabilityExecutor, ExecuteRequest, Manifest, OperationSpec
from harness.contracts.chat import MemoryUse, MemoryWrite, TurnDetail, TurnService
from harness.contracts.context import ContextPackage, ContextBuilder, ContextSection, InclusionRecord
from harness.contracts.consolidation import ConsolidationRun, ConsolidationService
from harness.contracts.domains import Domain, DomainRegistry, StructureDraft, Suggestion
from harness.contracts.events import Event, EventLog
from harness.contracts.history import HistoryNote, HistorySegment, HistoryService, HistoryView
from harness.contracts.loop import InteractionLoop, TurnEvent, TurnListener, TurnResult
from harness.contracts.maintenance import RemovedState, ResetReport, ResetService
from harness.contracts.memory import (
    ExecutionOutcomeRecorder,
    MemoryCandidate,
    MemoryManager,
    MemoryRecord,
    OutcomeCapture,
    RankedMemory,
    SubmitResult,
    UsageEntry,
)
from harness.contracts.model import Message, ModelAdapter, ModelResult
from harness.contracts.profile import Persona, PersonaDraft, PersonaProposal, PersonaVersion, SelfProfile
from harness.contracts.session import Interaction, Session, SessionManager
from harness.contracts.settings import SettingChange, SettingDef, SettingsService
from harness.contracts.sources import SourceResolver
from harness.contracts.tasks import Task, TaskManager, TaskOutcome, TaskSpec
from harness.contracts.traces import Trace, TraceStore
from harness.contracts.actions import (
    ActAction,
    AskAction,
    ContinueAction,
    EndAction,
    ExecutionResult,
    Proposal,
    RespondAction,
)

__all__ = [
    "ActAction",
    "AskAction",
    "CapabilityExecutor",
    "ConsolidationRun",
    "ConsolidationService",
    "ContextBuilder",
    "ContextPackage",
    "ContextSection",
    "ContinueAction",
    "Domain",
    "DomainRegistry",
    "EndAction",
    "Event",
    "EventLog",
    "ExecuteRequest",
    "ExecutionOutcomeRecorder",
    "ExecutionResult",
    "HistoryNote",
    "HistorySegment",
    "HistoryService",
    "HistoryView",
    "InclusionRecord",
    "Interaction",
    "InteractionLoop",
    "Manifest",
    "MemoryCandidate",
    "MemoryManager",
    "MemoryRecord",
    "MemoryUse",
    "MemoryWrite",
    "Message",
    "ModelAdapter",
    "ModelResult",
    "OperationSpec",
    "OutcomeCapture",
    "Persona",
    "PersonaDraft",
    "PersonaProposal",
    "PersonaVersion",
    "Proposal",
    "RankedMemory",
    "RemovedState",
    "ResetReport",
    "ResetService",
    "RespondAction",
    "SelfProfile",
    "Session",
    "SessionManager",
    "SettingChange",
    "SettingDef",
    "SettingsService",
    "SourceResolver",
    "StructureDraft",
    "SubmitResult",
    "Suggestion",
    "Task",
    "TaskManager",
    "TaskOutcome",
    "TaskSpec",
    "Trace",
    "TraceStore",
    "TurnDetail",
    "TurnEvent",
    "TurnListener",
    "TurnResult",
    "TurnService",
    "UsageEntry",
]

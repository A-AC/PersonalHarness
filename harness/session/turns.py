from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from harness.contracts.chat import (
    MemoryUse,
    MemoryWrite,
    TurnDetail,
    TurnEvent,
    TurnListener,
)
from harness.contracts.loop import InteractionLoop, TurnResult
from harness.contracts.memory import MemoryManager
from harness.contracts.settings import SettingsService
from harness.contracts.traces import Trace, TraceStore


def _emit(listener: TurnListener | None, kind: str, data: dict[str, Any]) -> None:
    if listener is None:
        return
    try:
        listener(TurnEvent(kind=kind, data=data))
    except Exception:
        # A dropped UI connection must not fail the turn.
        pass


class DefaultTurnService:
    """One turn entry point for both the terminal and the web UI.

    The service serializes turns (a local llama-server handles one request at a
    time anyway), forwards progress to an optional listener, and builds the
    read model the UI uses for trace/memory links.
    """

    def __init__(
        self,
        *,
        loop: InteractionLoop,
        traces: TraceStore,
        memory: MemoryManager,
        settings: SettingsService,
    ):
        self._loop = loop
        self._traces = traces
        self._memory = memory
        self._settings = settings
        self._lock = threading.Lock()
        self._blocked: str | None = None
        self._epoch = 0

    @contextmanager
    def exclusive(self, reason: str = "reset") -> Iterator[None]:
        """Stop new turns and wait for the running one before destructive work.

        The epoch bump also rejects turns that were already queued on the lock
        when the reset started, so they do not replay an old user request
        against the fresh state afterwards.
        """
        with self._lock:
            self._epoch += 1
            self._blocked = reason
            try:
                yield
            finally:
                self._blocked = None

    def submit(self, user_text: str, listener: TurnListener | None = None) -> TurnResult:
        epoch = self._epoch
        with self._lock:
            if self._blocked or epoch != self._epoch:
                error = (
                    f"turn rejected: harness is {self._blocked}"
                    if self._blocked
                    else "turn cancelled by a reset"
                )
                _emit(listener, "turn.failed", {"error": error, "interaction_id": ""})
                return TurnResult(interaction_id="", messages=[], error=error)
            try:
                result = self._loop.handle_turn(user_text, emit=listener)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                _emit(listener, "turn.failed", {"error": error, "interaction_id": ""})
                return TurnResult(interaction_id="", messages=[], error=error)
            detail = self.detail(result.interaction_id)
            payload = self._event_payload(detail, result)
            _emit(listener, "turn.failed" if result.error else "turn.completed", payload)
            return result

    def run_ready(self, *, limit: int, listener: TurnListener | None = None) -> TurnResult:
        """Run scheduler-eligible tasks under the same lock and epoch gate.

        Background work is serialized with user turns and rejected while a
        reset holds the epoch, so the scheduler can never race a wipe or a
        live conversation.
        """
        epoch = self._epoch
        with self._lock:
            if self._blocked or epoch != self._epoch:
                error = (
                    f"background run rejected: harness is {self._blocked}"
                    if self._blocked
                    else "background run cancelled by a reset"
                )
                return TurnResult(interaction_id="", messages=[], error=error)
            return self._loop.run_ready(limit=limit, emit=listener)

    def detail(self, interaction_id: str) -> TurnDetail:
        return self.detail_many([interaction_id]).get(interaction_id, TurnDetail(interaction_id=interaction_id))

    def detail_many(self, interaction_ids: list[str]) -> dict[str, TurnDetail]:
        """Build several turn details from one recent trace scan.

        A per-interaction scan reads every trace file, so a chat page with
        dozens of messages would otherwise do that work dozens of times.
        """
        unique = list(dict.fromkeys(item for item in interaction_ids if item))
        if not unique:
            return {}
        limit = int(self._settings.get("ui.page_limit"))
        scan = int(self._settings.get("ui.chat_trace_scan"))
        grouped: dict[str, list[Trace]] = {}
        if scan > 0:
            for trace in self._traces.list_for(limit=scan):
                grouped.setdefault(trace.interaction_id, []).append(trace)
        details: dict[str, TurnDetail] = {}
        for interaction_id in unique:
            traces = grouped.get(interaction_id)
            if traces is None:
                traces = self._traces.list_for(interaction_id=interaction_id, limit=limit)
            details[interaction_id] = self._build_detail(interaction_id, traces)
        return details

    def _build_detail(self, interaction_id: str, traces: list[Trace]) -> TurnDetail:
        memories: dict[str, MemoryUse] = {}
        writes: list[MemoryWrite] = []
        seen_writes: set[tuple[str, str, str]] = set()
        for trace in traces:
            for row in trace.included or []:
                item = str(row.get("item") or "")
                section = str(row.get("section") or "")
                if not item.startswith("mem_") or section not in {"domain", "preferences"}:
                    continue
                if item in memories:
                    continue
                memories[item] = self._memory_use(item, str(row.get("reason") or ""))
            for envelope in self._envelopes(trace):
                write = self._memory_write(envelope)
                if write is None:
                    continue
                key = (write.memory_id, write.operation, write.message)
                if key in seen_writes:
                    continue
                seen_writes.add(key)
                writes.append(write)
        error = None
        turn = next((item for item in traces if item.role == "turn"), None)
        if turn is not None:
            error = (turn.validation or {}).get("error")
        return TurnDetail(
            interaction_id=interaction_id,
            traces=traces,
            memories=list(memories.values()),
            writes=writes,
            error=str(error) if error else None,
        )

    def _memory_use(self, memory_id: str, reason: str) -> MemoryUse:
        try:
            record = self._memory.get(memory_id)
        except KeyError:
            return MemoryUse(id=memory_id, type="", scope="", content="(missing memory)", reason=reason)
        return MemoryUse(
            id=record.id,
            type=record.type,
            scope=record.scope,
            content=record.content,
            reason=reason,
        )

    @staticmethod
    def _envelopes(trace: Trace) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        envelope = (trace.validation or {}).get("envelope")
        if isinstance(envelope, dict):
            items.append(envelope)
        for stage in trace.timeline or []:
            if stage.get("stage") != "execution":
                continue
            detail = stage.get("detail")
            if isinstance(detail, dict) and detail.get("operation_id") not in {
                item.get("operation_id") for item in items
            }:
                items.append(detail)
        return items

    @staticmethod
    def _memory_write(envelope: dict[str, Any]) -> MemoryWrite | None:
        if envelope.get("capability") != "harness_memory":
            return None
        if envelope.get("status") != "succeeded":
            return None
        data = envelope.get("data") or {}
        memory_id = data.get("memory_id")
        if not memory_id or data.get("duplicate"):
            return None
        restore: list[str] = []
        for item in data.get("replaced") or []:
            if isinstance(item, dict) and item.get("id"):
                restore.append(str(item["id"]))
        if data.get("supersedes"):
            restore.append(str(data["supersedes"]))
        return MemoryWrite(
            memory_id=str(memory_id),
            operation=str(envelope.get("operation") or ""),
            status=str(envelope.get("status") or ""),
            message=str(envelope.get("message") or ""),
            restore_ids=restore,
            undo="restore" if envelope.get("operation") == "retract" else "retract",
        )

    @staticmethod
    def _event_payload(detail: TurnDetail, result: TurnResult) -> dict[str, Any]:
        traces = [_trace_payload(item) for item in detail.traces]
        return {
            "interaction_id": result.interaction_id,
            "messages": result.messages,
            "error": result.error or detail.error,
            "task_ids": [task.id for task in result.tasks],
            "trace_ids": detail.trace_ids,
            "traces": traces,
            "memories": [
                {
                    "id": item.id,
                    "type": item.type,
                    "scope": item.scope,
                    "content": item.content,
                    "reason": item.reason,
                }
                for item in detail.memories
            ],
            "writes": [
                {
                    "memory_id": item.memory_id,
                    "operation": item.operation,
                    "status": item.status,
                    "message": item.message,
                    "restore_ids": item.restore_ids,
                    "undo": item.undo,
                }
                for item in detail.writes
            ],
        }


def _trace_payload(trace: Trace) -> dict[str, Any]:
    envelope = (trace.validation or {}).get("envelope")
    duration = trace.duration_ms if trace.duration_ms is not None else trace.latency_ms
    return {
        "id": trace.id,
        "role": trace.role,
        "task_id": trace.task_id,
        "duration_ms": duration,
        "validation": trace.validation,
        "envelope": envelope,
    }

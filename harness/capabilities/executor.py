from __future__ import annotations

import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

from harness.constants import ExecutionStatus, PermissionLevel
from harness.contracts.actions import ExecutionResult
from harness.contracts.capabilities import ExecuteRequest, Manifest, OperationSpec
from harness.contracts.domains import DomainRegistry
from harness.contracts.events import Event, EventLog
from harness.contracts.memory import MemoryCandidate, MemoryManager
from harness.contracts.settings import SettingsService
from harness.capabilities.harness_ops import MEMORY_MANIFEST, STRUCTURE_MANIFEST
from harness.capabilities.mcp import McpServerSpec, call_mcp_tool, load_mcp_manifests
from harness.capabilities.mock import MOCK_MANIFEST, run_mock
from harness.capabilities.skills import load_skills
from harness.storage.atomic import atomic_write_json, new_id, now_ts, read_json


class FileCapabilityExecutor:
    def __init__(
        self,
        directory: Path,
        settings: SettingsService,
        events: EventLog,
        domains: DomainRegistry,
        memory: MemoryManager | None = None,
        structure_hook: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self._dir = directory
        self._attempts_dir = directory / "attempts"
        self._attempts_dir.mkdir(parents=True, exist_ok=True)
        self._settings = settings
        self._events = events
        self._domains = domains
        self._memory = memory
        self._structure_hook = structure_hook
        self._scripts: dict[str, list[dict[str, Any]]] = {}
        self._manifests: dict[str, Manifest] = {m.name: m for m in [MOCK_MANIFEST, MEMORY_MANIFEST, STRUCTURE_MANIFEST]}
        self._mcp_specs: dict[str, McpServerSpec] = {}
        skills_dir = str(settings.get("capabilities.skills_dir") or "")
        if skills_dir:
            for manifest in load_skills(Path(skills_dir)):
                self._manifests[manifest.name] = manifest
        mcp_file = str(settings.get("capabilities.mcp_servers_file") or "")
        if mcp_file:
            loaded, specs = load_mcp_manifests(Path(mcp_file), float(settings.get("capabilities.mcp_timeout_s")))
            self._manifests.update(loaded)
            self._mcp_specs.update(specs)

    def attach_memory(self, memory: MemoryManager) -> None:
        self._memory = memory

    def attach_structure(self, hook: Callable[[str, dict[str, Any]], dict[str, Any]]) -> None:
        self._structure_hook = hook

    def list_manifests(self) -> list[Manifest]:
        return list(self._manifests.values())

    def get(self, name: str) -> Manifest:
        if name not in self._manifests:
            raise KeyError(name)
        return self._manifests[name]

    def script_next(self, capability: str, envelope: dict[str, Any]) -> None:
        self._scripts.setdefault(capability, []).append(envelope)

    def last_attempts(self, task_id: str) -> list[ExecutionResult]:
        items = []
        for path in self._attempts_dir.glob("op_*.json"):
            data = read_json(path)
            if data.get("task_id") == task_id:
                items.append(ExecutionResult(**data))
        items.sort(key=lambda item: (
            (item.evidence or {}).get("started_at", ""),
            (item.evidence or {}).get("sequence", 0),
        ))
        return items

    def get_attempt(self, operation_id: str) -> ExecutionResult | None:
        path = self._attempts_dir / f"{operation_id}.json"
        if not path.exists():
            return None
        return ExecutionResult(**read_json(path))

    def execute(self, req: ExecuteRequest) -> ExecutionResult:
        operation_id = new_id("op")
        started = now_ts()
        sequence = time.time_ns()
        pending = self._result(operation_id, req, ExecutionStatus.UNKNOWN, {}, "in flight", "in_flight")
        pending.evidence = {"started_at": started, "ended_at": "", "confirmation": "in_flight", "sequence": sequence}
        retries = int(self._settings.get("storage.atomic_retries"))
        retry_s = float(self._settings.get("storage.atomic_retry_s"))
        atomic_write_json(self._attempts_dir / f"{pending.operation_id}.json", asdict(pending), retries=retries, retry_s=retry_s)
        if req.capability not in self._manifests:
            result = self._result(operation_id, req, ExecutionStatus.UNAVAILABLE, {}, "capability not registered", "not_registered")
            return self._finish(result, started, sequence)
        manifest = self._manifests[req.capability]
        if not manifest.available:
            result = self._result(
                operation_id,
                req,
                ExecutionStatus.UNAVAILABLE,
                {},
                f"capability {req.capability} is unavailable",
                "unavailable",
            )
            return self._finish(result, started, sequence)
        op = self._operation(manifest, req.operation)
        if op is None:
            result = self._result(operation_id, req, ExecutionStatus.UNAVAILABLE, {}, "operation not found", "not_found")
            return self._finish(result, started, sequence)
        if op.accepts_idempotency_key:
            req = replace(req, idempotency_key=operation_id)
        enabled = []
        if req.domain_id:
            try:
                enabled = self._domains.enablement(req.domain_id)
            except KeyError:
                enabled = []
        else:
            raw = str(self._settings.get("capabilities.default_enabled") or "")
            enabled = [item.strip() for item in raw.split(",") if item.strip()]
        if req.capability not in enabled:
            result = self._result(operation_id, req, ExecutionStatus.DENIED, {}, "capability not enabled for domain", "not_enabled")
            return self._finish(result, started, sequence)
        if not req.permission_override and not self._permitted(op.permission, req.capability):
            result = self._result(
                operation_id,
                req,
                ExecutionStatus.DENIED,
                {},
                f"permission denied by capabilities.policy.{op.permission}",
                "permission_denied",
            )
            return self._finish(result, started, sequence)
        arg_error = self._validate_args(op, req.arguments)
        if arg_error:
            result = self._result(operation_id, req, ExecutionStatus.FAILED, {}, arg_error, "invalid_arguments")
            return self._finish(result, started, sequence)

        scripted = None
        queue = self._scripts.get(req.capability) or []
        if queue:
            scripted = queue.pop(0)
        try:
            payload = self._dispatch(req, scripted)
        except Exception as exc:  # unexpected capability failure
            result = self._result(operation_id, req, ExecutionStatus.FAILED, {}, str(exc), "exception")
            return self._finish(result, started, sequence)

        status = payload.get("status", ExecutionStatus.FAILED)
        result = self._result(
            operation_id,
            req,
            status,
            payload.get("data") or {},
            payload.get("message") or "",
            payload.get("error_category"),
        )
        if scripted and scripted.get("status") == ExecutionStatus.UNKNOWN:
            result.status = ExecutionStatus.UNKNOWN
            result.message = scripted.get("message") or "unknown outcome"
        return self._finish(result, started, sequence)

    def _permitted(self, permission: str, capability: str) -> bool:
        if capability.startswith("harness_"):
            return True
        policy = str(self._settings.get(f"capabilities.policy.{permission}"))
        return policy == "allow"

    def _operation(self, manifest: Manifest, name: str) -> OperationSpec | None:
        for item in manifest.operations:
            if item.name == name:
                return item
        return None

    def _validate_args(self, op: OperationSpec, arguments: dict[str, Any]) -> str | None:
        if not isinstance(arguments, dict):
            return "arguments must be an object"
        for key in op.required:
            if key not in arguments:
                return f"missing required argument: {key}"
        for key, value in arguments.items():
            expected = op.inputs.get(key)
            if expected is None:
                return f"unexpected argument: {key}"
            if expected == "string" and not isinstance(value, str):
                return f"{key} must be a string"
            if expected == "number" and (
                not isinstance(value, (int, float)) or isinstance(value, bool)
            ):
                return f"{key} must be a number"
            if expected == "boolean" and not isinstance(value, bool):
                return f"{key} must be a boolean"
        return None

    def _dispatch(self, req: ExecuteRequest, scripted: dict[str, Any] | None) -> dict[str, Any]:
        if scripted is not None and req.capability != "harness_memory" and req.capability != "harness_structure":
            if req.idempotency_key and "data" in scripted:
                data = dict(scripted.get("data") or {})
                data.setdefault("idempotency_key", req.idempotency_key)
                scripted = {**scripted, "data": data}
            return scripted
        if req.capability == "mock":
            return run_mock(req.operation, req.arguments, scripted, idempotency_key=req.idempotency_key)
        if req.capability == "harness_memory":
            return self._memory_op(req)
        if req.capability == "harness_structure":
            if self._structure_hook is None:
                return {"status": ExecutionStatus.UNAVAILABLE, "data": {}, "message": "structure hook missing"}
            return self._structure_hook(req.operation, req.arguments)
        if req.capability in self._mcp_specs:
            return call_mcp_tool(
                self._mcp_specs[req.capability],
                req.operation,
                req.arguments,
                float(self._settings.get("capabilities.mcp_timeout_s")),
            )
        return {"status": ExecutionStatus.UNAVAILABLE, "data": {}, "message": "no executor for capability"}

    def _memory_op(self, req: ExecuteRequest) -> dict[str, Any]:
        if self._memory is None:
            return {"status": ExecutionStatus.UNAVAILABLE, "data": {}, "message": "memory manager missing"}
        args = req.arguments
        if req.operation == "remember":
            result = self._memory.submit(
                MemoryCandidate(
                    type=str(args.get("type") or "fact"),
                    content=str(args["content"]),
                    scope=str(args.get("scope") or "global"),
                    origin="explicit",
                    provenance={
                        "source_kind": "user_statement",
                        "source_refs": [req.interaction_id or req.task_id],
                        "evidence_status": "stated",
                    },
                )
            )
            if not result.accepted:
                return {"status": ExecutionStatus.FAILED, "data": {}, "message": result.reason, "error_category": "validation"}
            return {"status": ExecutionStatus.SUCCEEDED, "data": {"memory_id": result.record.id if result.record else None}, "message": "remembered"}
        if req.operation == "correct":
            old = self._memory.get(str(args["memory_id"]))
            result = self._memory.submit(
                MemoryCandidate(
                    type=old.type,
                    content=str(args["content"]),
                    scope=old.scope,
                    origin="explicit",
                    provenance={
                        "source_kind": "user_statement",
                        "source_refs": [req.interaction_id or req.task_id],
                        "evidence_status": "stated",
                    },
                    supersedes=old.id,
                )
            )
            if not result.accepted or result.record is None:
                return {"status": ExecutionStatus.FAILED, "data": {}, "message": result.reason}
            return {"status": ExecutionStatus.SUCCEEDED, "data": {"memory_id": result.record.id, "supersedes": old.id}, "message": "corrected"}
        if req.operation == "retract":
            record = self._memory.retract(str(args["memory_id"]), "user")
            return {"status": ExecutionStatus.SUCCEEDED, "data": {"memory_id": record.id}, "message": "retracted"}
        return {"status": ExecutionStatus.FAILED, "data": {}, "message": "unknown memory operation"}

    def _result(self, operation_id: str, req: ExecuteRequest, status: str, data: dict, message: str, error_category: str | None) -> ExecutionResult:
        return ExecutionResult(
            operation_id=operation_id,
            task_id=req.task_id,
            capability=req.capability,
            operation=req.operation,
            status=status,
            data=data,
            message=message,
            error_category=error_category,
            evidence={"started_at": "", "ended_at": ""},
        )

    def _finish(self, result: ExecutionResult, started: str, sequence: int) -> ExecutionResult:
        ended = now_ts()
        result.evidence = {
            "started_at": started,
            "ended_at": ended,
            "confirmation": result.status,
            "sequence": sequence,
        }
        if result.operation_id:
            result.evidence["operation_id"] = result.operation_id
        retries = int(self._settings.get("storage.atomic_retries"))
        retry_s = float(self._settings.get("storage.atomic_retry_s"))
        atomic_write_json(self._attempts_dir / f"{result.operation_id}.json", asdict(result), retries=retries, retry_s=retry_s)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=ended,
                actor="harness",
                operation="capability.execute",
                target=result.operation_id,
                result=result.status,
                refs=[result.task_id],
                data={"capability": result.capability, "operation": result.operation, "error_category": result.error_category},
            )
        )
        return result

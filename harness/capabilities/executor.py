from __future__ import annotations

import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

from harness.constants import SELF_DOMAIN_ID, ExecutionStatus, MemoryScopeKind, PermissionLevel
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
from harness.text import UNRESOLVED_PLACEHOLDER_RE


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
        normalized, arg_errors, notes = self._normalize_args(op, req.arguments)
        if arg_errors:
            result = self._result(
                operation_id,
                req,
                ExecutionStatus.FAILED,
                {"argument_errors": arg_errors, "argument_notes": notes},
                "invalid arguments: " + "; ".join(arg_errors),
                "invalid_arguments",
            )
            return self._finish(result, started, sequence)
        req = replace(req, arguments=normalized)

        scripted = None
        queue = self._scripts.get(req.capability) or []
        if queue:
            scripted = queue.pop(0)
        try:
            payload = self._dispatch(req, scripted)
        except Exception as exc:  # unexpected capability failure
            result = self._result(operation_id, req, ExecutionStatus.FAILED, {}, str(exc), "exception")
            return self._finish(result, started, sequence)
        if notes:
            data = dict(payload.get("data") or {})
            existing = data.get("argument_notes")
            data["argument_notes"] = [*(existing or []), *notes]
            payload = {**payload, "data": data}

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

    def _normalize_args(
        self, op: OperationSpec, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        """Validate every argument at once and normalize optional hints.

        Required arguments are gates: a wrong value fails with the field, the
        rejected value, and the allowed values. Optional arguments are hints:
        an unusable enum value falls back to its default (or is omitted so the
        dispatcher can derive it), and the adjustment is recorded as a note.
        """
        if not isinstance(arguments, dict):
            return {}, ["arguments must be an object"], []
        normalized = dict(arguments)
        errors: list[str] = []
        notes: list[str] = []
        coerce_setting = bool(self._settings.get("capabilities.coerce_optional_arguments"))
        allowed_names = ", ".join(op.inputs) or "none"

        for key in op.required:
            if key not in normalized:
                errors.append(f"missing required argument: {key}")

        for key, value in list(normalized.items()):
            expected = op.inputs.get(key)
            if expected is None:
                errors.append(f'unexpected argument "{key}"; allowed: {allowed_names}')
                continue
            required = key in op.required
            # Hint inputs are model guesses, never user decisions. A bad hint
            # must not fail the call even when general coercion is disabled.
            coerce_optional = coerce_setting or key in op.hint_inputs
            enum = op.enums.get(key)
            if enum:
                text = value if isinstance(value, str) else str(value)
                if not self._enum_allowed(text, enum):
                    if key in op.hint_inputs:
                        # Leave the raw hint in place: the owning component
                        # knows the actual fallback and can record a precise
                        # note (for example the classified memory type).
                        continue
                    if required or not coerce_optional:
                        errors.append(
                            f'invalid {key} "{text}"; allowed: {", ".join(enum)}'
                        )
                    else:
                        fallback = op.defaults.get(key)
                        if fallback is None:
                            normalized.pop(key, None)
                        else:
                            normalized[key] = fallback
                        notes.append(
                            f'{key} "{text}" is not allowed; used '
                            f'{fallback if fallback is not None else "the harness-derived default"}'
                        )
                    continue
                normalized[key] = text
                continue
            if expected == "string":
                if not isinstance(value, str):
                    if required or not coerce_optional:
                        errors.append(f'invalid {key} "{value}": must be a string')
                    else:
                        normalized[key] = str(value)
                        notes.append(f"{key} was converted to a string")
                elif required and bool(self._settings.get("capabilities.reject_placeholders")):
                    placeholder = UNRESOLVED_PLACEHOLDER_RE.search(value)
                    if placeholder:
                        errors.append(
                            f'invalid {key} "{value}": contains unresolved placeholder '
                            f"{placeholder.group(0)}; ask the user for the real value"
                        )
            elif expected == "number":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    if required or not coerce_optional:
                        errors.append(f'invalid {key} "{value}": must be a number')
                    else:
                        try:
                            normalized[key] = float(value)
                            notes.append(f"{key} was converted to a number")
                        except (TypeError, ValueError):
                            normalized.pop(key, None)
                            notes.append(f"{key} was not a number and was omitted")
            elif expected == "boolean":
                if not isinstance(value, bool):
                    if required or not coerce_optional:
                        errors.append(f'invalid {key} "{value}": must be a boolean')
                    else:
                        normalized[key] = str(value).strip().lower() in {"1", "true", "yes", "on"}
                        notes.append(f"{key} was converted to a boolean")

        for key, default in op.defaults.items():
            if key not in normalized and key not in op.required:
                normalized[key] = default
        return normalized, errors, notes

    @staticmethod
    def _enum_allowed(value: str, allowed: tuple[str, ...]) -> bool:
        for item in allowed:
            if item.endswith(":<id>"):
                prefix = item.split(":<id>", 1)[0] + ":"
                # The literal pattern shown in the prompt is not an id, and an
                # unresolved placeholder is not either.
                if (
                    len(value) > len(prefix)
                    and value.startswith(prefix)
                    and not UNRESOLVED_PLACEHOLDER_RE.search(value)
                ):
                    return True
                continue
            if item == value:
                return True
        return False

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
        ref = req.interaction_id or ""
        provenance = {
            "source_kind": "user_statement",
            "source_refs": [ref] if ref else [],
            "evidence_status": "stated",
        }
        if req.operation == "remember":
            result = self._memory.submit(
                MemoryCandidate(
                    content=str(args["content"]),
                    origin="explicit",
                    type_hint=args.get("type"),
                    scope_hint=args.get("scope"),
                    default_scope=self._derived_scope(req),
                    provenance=provenance,
                )
            )
            if not result.accepted or result.record is None:
                return {"status": ExecutionStatus.FAILED, "data": {}, "message": result.reason, "error_category": "validation"}
            record = result.record
            data: dict[str, Any] = {
                "memory_id": record.id,
                "kind": record.kind,
                "type": record.type,
                "scope": record.scope,
            }
            if result.duplicate:
                data["duplicate"] = True
            if result.replaced:
                data["replaced"] = [{"id": item.id, "content": item.content} for item in result.replaced]
            if result.hint_notes:
                data["argument_notes"] = list(result.hint_notes)
            message = "already remembered" if result.duplicate else "remembered"
            if result.replaced:
                message = f'replaced previous memory: "{result.replaced[0].content}"'
            return {"status": ExecutionStatus.SUCCEEDED, "data": data, "message": message}
        if req.operation == "correct":
            try:
                old = self._memory.get(str(args["memory_id"]))
            except KeyError:
                return {"status": ExecutionStatus.FAILED, "data": {}, "message": "memory_id not found", "error_category": "not_found"}
            result = self._memory.submit(
                MemoryCandidate(
                    content=str(args["content"]),
                    origin="explicit",
                    type_hint=old.type,
                    kind_hint=old.kind,
                    scope_hint=old.scope,
                    default_scope=old.scope,
                    provenance=provenance,
                    supersedes=old.id,
                )
            )
            if not result.accepted or result.record is None:
                return {"status": ExecutionStatus.FAILED, "data": {}, "message": result.reason, "error_category": "validation"}
            return {"status": ExecutionStatus.SUCCEEDED, "data": {"memory_id": result.record.id, "supersedes": old.id}, "message": "corrected"}
        if req.operation == "retract":
            try:
                record = self._memory.retract(str(args["memory_id"]), "user")
            except KeyError:
                return {"status": ExecutionStatus.FAILED, "data": {}, "message": "memory_id not found", "error_category": "not_found"}
            return {"status": ExecutionStatus.SUCCEEDED, "data": {"memory_id": record.id}, "message": "retracted"}
        return {"status": ExecutionStatus.FAILED, "data": {}, "message": "unknown memory operation"}

    @staticmethod
    def _derived_scope(req: ExecuteRequest) -> str:
        """The authoritative default scope, independent of the model hint."""
        if req.project_id:
            return f"project:{req.project_id}"
        if req.domain_id == SELF_DOMAIN_ID or req.self_request:
            return MemoryScopeKind.SELF.value
        if req.domain_id:
            return f"domain:{req.domain_id}"
        return MemoryScopeKind.GLOBAL.value

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

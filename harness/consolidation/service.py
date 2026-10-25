from __future__ import annotations

import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from harness.constants import MemoryOrigin
from harness.contracts.consolidation import ConsolidationRun
from harness.contracts.domains import DomainRegistry, StructureDraft
from harness.contracts.events import Event, EventLog
from harness.contracts.memory import MemoryCandidate, MemoryManager
from harness.contracts.model import Message, ModelAdapter, ModelUnavailable
from harness.contracts.profile import PersonaDraft, SelfProfile
from harness.contracts.session import SessionManager
from harness.contracts.settings import SettingsService
from harness.storage.atomic import atomic_write_json, new_id, now_ts, read_json


CONSOLIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "content": {"type": "string"},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "scope": {"type": ["string", "null"]},
                    "type": {"type": ["string", "null"]},
                    "supersedes": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "name": {"type": "string"},
                    "purpose": {"type": "string"},
                    "communication_style": {"type": "string"},
                    "operating_principles": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                    "finding": {"type": "string"},
                    "structure_kind": {"type": "string"},
                },
            },
        },
        "findings": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["proposals"],
}


INSTRUCTIONS = """
Read the recent conversation and propose durable memories worth keeping.
Return JSON: {"proposals":[...],"findings":[{"message":"..."}]}.
Each memory proposal is {"kind":"new_memory","content":"clear standalone statement",
"source_refs":["int_..."],"confidence":0.0}.
Copy source_refs from the interaction ids in the evidence; the harness verifies them.
Only propose stable facts, preferences, decisions, procedures, or lessons that will
still be useful later. Do not restate an existing active memory, and do not propose
temporary state, greetings, or the assistant's own text. Type and scope are optional
hints; never ask the user to choose them. Do not restate a capability rule or schema
as a memory; return those as findings.
"""


class FileConsolidationService:
    def __init__(
        self,
        directory: Path,
        settings: SettingsService,
        model: ModelAdapter,
        memory: MemoryManager,
        sessions: SessionManager,
        events: EventLog,
        profile: SelfProfile,
        domains: DomainRegistry,
    ):
        self._dir = directory
        self._settings = settings
        self._model = model
        self._memory = memory
        self._sessions = sessions
        self._events = events
        self._profile = profile
        self._domains = domains
        self._lock = threading.RLock()
        directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Wait for an in-flight run before reset wipes memory/run state."""
        with self._lock:
            yield

    def run(self, *, trigger: str) -> ConsolidationRun:
        with self._lock:
            return self._run_locked(trigger=trigger)

    def _run_locked(self, *, trigger: str) -> ConsolidationRun:
        run_id = new_id("run")
        started = now_ts()
        run = ConsolidationRun(
            id=run_id,
            started_at=started,
            ended_at=None,
            trigger=trigger,
            status="running",
            input_snapshot_ref=f"{run_id}.input.json",
        )
        self._save(run)
        evidence = self._evidence()
        self._save_snapshot(run, evidence)

        # Inference first, then deterministic maintenance over the result so a
        # newly extracted memory is re-typed and de-duplicated in the same run.
        extraction = self._extract(run, run_id, evidence)
        run.results.extend(self._memory.consolidate(run_id, "consolidation"))
        run.report = self._report(run.results, extraction)
        run.status = "completed"
        run.ended_at = now_ts()
        self._save(run)
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=run.ended_at,
                actor="consolidation",
                operation="consolidation.run",
                target=run.id,
                result=run.status,
                data=run.report,
            )
        )
        return run

    def _extract(self, run: ConsolidationRun, run_id: str, evidence: str) -> str:
        if not bool(self._settings.get("consolidation.extract_enabled")):
            run.findings.append({"message": "inference extraction disabled"})
            return "disabled"
        if not self._model.health("background"):
            run.findings.append({"message": "background model unavailable; deterministic maintenance only"})
            return "unavailable"
        interactions = self._sessions.list_interactions(limit=int(self._settings.get("consolidation.evidence_interactions")))
        if not interactions:
            run.findings.append({"message": "no recent interactions to extract from"})
            return "no_evidence"
        try:
            result = self._model.complete(
                "background",
                [Message(role="system", content=INSTRUCTIONS), Message(role="user", content=evidence)],
                schema=CONSOLIDATION_SCHEMA,
            )
        except ModelUnavailable as exc:
            run.findings.append({"message": str(exc)})
            return "unavailable"
        payload = self._parse(result.content)
        proposals = [item for item in (payload.get("proposals") or []) if isinstance(item, dict)]
        run.proposals = proposals
        run.findings.extend(item for item in (payload.get("findings") or []) if isinstance(item, dict))
        limit = int(self._settings.get("consolidation.max_proposals"))
        for proposal in proposals[:limit]:
            run.results.append(self._apply(run_id, proposal, interactions))
        for proposal in proposals[limit:]:
            run.results.append({"kind": str(proposal.get("kind") or ""), "status": "skipped_limit"})
        self._events.append(
            Event(
                id=new_id("evt"),
                ts=now_ts(),
                actor="model",
                operation="model.call",
                target=run_id,
                result="ok",
                refs=[run.id],
                data={"role": "background", "latency_ms": result.latency_ms, "tokens": result.completion_tokens},
            )
        )
        return "applied"

    def get(self, run_id: str) -> ConsolidationRun:
        return ConsolidationRun(**read_json(self._dir / f"{run_id}.json"))

    def list(self, limit: int = 20) -> list[ConsolidationRun]:
        items = [ConsolidationRun(**read_json(path)) for path in sorted(self._dir.glob("run_*.json"))]
        return items[-limit:]

    def rollback(self, run_id: str, actor: str) -> ConsolidationRun:
        run = self.get(run_id)
        self._memory.rollback_run(run_id, actor)
        for result in run.results:
            if result.get("kind") == "retype" and result.get("memory_id") and result.get("from"):
                try:
                    self._memory.set_type(str(result["memory_id"]), str(result["from"]), actor)
                except (KeyError, ValueError):
                    pass
            if result.get("kind") in {"merged", "superseded"} and result.get("loser_id"):
                try:
                    loser = self._memory.get(str(result["loser_id"]))
                    if loser.status != "active":
                        self._memory.restore(loser.id, actor)
                except KeyError:
                    pass
            if result.get("kind") == "persona_edit" and result.get("id"):
                try:
                    self._profile.reject(str(result["id"]), actor)
                except (KeyError, ValueError):
                    pass
            if result.get("kind") == "domain_suggestion" and result.get("suggestion_id"):
                try:
                    self._domains.decline_suggestion(str(result["suggestion_id"]))
                except (KeyError, ValueError):
                    pass
        run.rolled_back = True
        run.status = "rolled_back"
        self._save(run)
        self._events.append(
            Event(id=new_id("evt"), ts=now_ts(), actor=actor, operation="consolidation.rollback", target=run_id, result="ok")
        )
        return run

    def _save(self, run: ConsolidationRun) -> None:
        retries = int(self._settings.get("storage.atomic_retries"))
        retry_s = float(self._settings.get("storage.atomic_retry_s"))
        atomic_write_json(self._dir / f"{run.id}.json", asdict(run), retries=retries, retry_s=retry_s)

    def _save_snapshot(self, run: ConsolidationRun, evidence: str) -> None:
        retries = int(self._settings.get("storage.atomic_retries"))
        retry_s = float(self._settings.get("storage.atomic_retry_s"))
        atomic_write_json(
            self._dir / run.input_snapshot_ref,
            {"run_id": run.id, "ts": run.started_at, "evidence": evidence},
            retries=retries,
            retry_s=retry_s,
        )

    def _parse(self, raw: str) -> dict[str, Any]:
        import json

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"proposals": [], "findings": [{"message": "invalid consolidation JSON"}]}
        if not isinstance(data, dict):
            return {"proposals": [], "findings": [{"message": "consolidation payload must be an object"}]}
        return data

    def _evidence(self) -> str:
        interactions = self._sessions.list_interactions(limit=int(self._settings.get("consolidation.evidence_interactions")))
        usage = self._memory.usage_log(limit=int(self._settings.get("consolidation.evidence_usage")))
        events = self._events.query(limit=int(self._settings.get("consolidation.evidence_events")))
        lines = ["# interactions"]
        for item in interactions:
            lines.append(f"{item.id}: user={item.user_text} assistant={item.assistant_text}")
        lines.append("# usage")
        for item in usage:
            lines.append(f"{item.memory_id} task={item.task_id} reason={item.reason}")
        lines.append("# events")
        for item in events:
            lines.append(f"{item.id} {item.operation} {item.result} {item.target}")
        lines.append("# active memories")
        for item in self._memory.list(status="active"):
            lines.append(f"{item.id} {item.type} {item.origin} {item.content}")
        return "\n".join(lines)

    def _apply(self, run_id: str, proposal: dict[str, Any], interactions: list[Any]) -> dict[str, Any]:
        kind = str(proposal.get("kind") or "new_memory")
        if kind == "finding" or proposal.get("finding"):
            return {"kind": kind, "status": "finding", "message": proposal.get("finding") or proposal.get("content")}
        content = str(proposal.get("content") or "").strip()
        if self._structural_finding(content):
            return {"kind": kind, "status": "finding", "message": "structural defect; not stored as memory"}
        if kind == "domain_suggestion":
            draft = StructureDraft(
                kind="project" if proposal.get("structure_kind") == "project" else "domain",
                name=str(proposal.get("name") or content or "untitled"),
                reason=str(proposal.get("reason") or "consolidation suggestion"),
                evidence=f"consolidation run {run_id}",
            )
            suggestion = self._domains.record_suggestion(draft)
            return {"kind": kind, "status": "queued", "suggestion_id": suggestion.id}
        if kind == "persona_edit":
            draft = PersonaDraft(
                name=str(proposal.get("name") or self._profile.current().name),
                purpose=str(proposal.get("purpose") or self._profile.current().purpose),
                communication_style=str(proposal.get("communication_style") or self._profile.current().communication_style),
                operating_principles=list(proposal.get("operating_principles") or self._profile.current().operating_principles),
                reason=str(proposal.get("reason") or "consolidation"),
            )
            item = self._profile.propose_edit(draft, "consolidation")
            return {"kind": kind, "status": "queued", "id": item.id}
        if kind in {"new_memory", "merge", "confidence"}:
            if not content:
                return {"kind": kind, "status": "rejected", "reason": "empty content"}
            valid = {item.id for item in interactions}
            refs = [str(item) for item in (proposal.get("source_refs") or []) if str(item) in valid]
            if not refs and interactions:
                refs = [interactions[-1].id]
            if not refs:
                return {"kind": kind, "status": "rejected", "reason": "no source interaction"}
            origin = MemoryOrigin.CONSOLIDATED if kind == "confidence" else MemoryOrigin.INFERRED
            submitted = self._memory.submit(
                MemoryCandidate(
                    content=content,
                    origin=origin,
                    type_hint=proposal.get("type"),
                    scope_hint=proposal.get("scope"),
                    default_scope=str(self._settings.get("memory.default_scope")),
                    provenance={
                        "source_kind": "user_statement",
                        "source_refs": refs,
                        "evidence_status": "inferred",
                        "confidence": proposal.get("confidence"),
                    },
                    supersedes=proposal.get("supersedes"),
                    run_id=run_id,
                )
            )
            status = "committed" if submitted.accepted else "rejected"
            if submitted.duplicate:
                status = "duplicate"
            elif submitted.replaced:
                status = "superseded"
            return {
                "kind": kind,
                "status": status,
                "reason": submitted.reason,
                "content": content,
                "memory_id": submitted.record.id if submitted.record else None,
                "winner_id": submitted.record.id if submitted.record else None,
                "loser_id": submitted.replaced[0].id if submitted.replaced else proposal.get("supersedes"),
                "hint_notes": submitted.hint_notes,
            }
        return {"kind": kind, "status": "ignored"}

    def _structural_finding(self, content: str) -> bool:
        text = content.lower()
        needles = ("required argument", "missing required", "manifest already", "schema already", "validation gap")
        return any(item in text for item in needles)

    def _report(self, results: list[dict[str, Any]], extraction: str) -> dict[str, Any]:
        added = [
            {"memory_id": item.get("memory_id"), "content": item.get("content"), "kind": item.get("kind")}
            for item in results
            if item.get("status") == "committed" and item.get("memory_id")
        ]
        merged = [
            {"winner_id": item.get("winner_id"), "loser_id": item.get("loser_id")}
            for item in results
            if item.get("kind") == "merged"
        ]
        superseded = [
            {"winner_id": item.get("winner_id"), "loser_id": item.get("loser_id"), "memory_id": item.get("memory_id")}
            for item in results
            if item.get("kind") == "superseded" or item.get("status") == "superseded"
        ]
        retyped = [
            {"memory_id": item.get("memory_id"), "from": item.get("from"), "to": item.get("to")}
            for item in results
            if item.get("kind") == "retype"
        ]
        counts = Counter(str(item.get("status") or item.get("kind") or "unknown") for item in results)
        return {
            "added": added,
            "merged": merged,
            "superseded": superseded,
            "retyped": retyped,
            "counts": dict(sorted(counts.items())),
            "extraction": extraction,
        }

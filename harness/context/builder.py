from __future__ import annotations

from harness.constants import MemoryType, SELF_DOMAIN_ID
from harness.contracts.capabilities import CapabilityExecutor
from harness.contracts.context import ContextPackage, ContextSection, InclusionRecord
from harness.contracts.decisions import Decision, DecisionStore
from harness.contracts.domains import DomainInference, DomainRegistry
from harness.contracts.history import HistoryService
from harness.contracts.memory import MemoryManager, RankedMemory, UsageEntry
from harness.contracts.model import Message
from harness.contracts.profile import SelfProfile
from harness.contracts.session import Session, SessionManager
from harness.contracts.settings import SettingsService
from harness.contracts.tasks import Task
from harness.prompts import BASE_INSTRUCTIONS, TRIMMED_INSTRUCTIONS
from harness.storage.atomic import now_ts
from harness.text import estimate_tokens, matches_keywords
from harness.timefmt import (
    current_time_block,
    gap_between,
    human_gap,
    time_marker,
    timezone_name,
)


SECTION_ORDER = [
    "base_instructions",
    "persona",
    "preferences",
    "structure",
    "domain",
    "task",
    "history",
    "capabilities",
]


class DefaultContextBuilder:
    def __init__(
        self,
        settings: SettingsService,
        memory: MemoryManager,
        profile: SelfProfile,
        domains: DomainRegistry,
        capabilities: CapabilityExecutor,
        sessions: SessionManager,
        history: HistoryService,
        decisions: DecisionStore | None = None,
        inference: DomainInference | None = None,
    ):
        self._settings = settings
        self._memory = memory
        self._profile = profile
        self._domains = domains
        self._capabilities = capabilities
        self._sessions = sessions
        self._history = history
        self._decisions = decisions
        self._inference = inference

    def build(self, task: Task, session: Session, *, trace_id: str | None = None) -> ContextPackage:
        persona = self._profile.current()
        persona_text = self._persona_block(persona)
        pref_records = self._memory.list(scope="global", status="active", type=MemoryType.PREFERENCE)
        assistant_request = self._is_assistant_request(task.goal)
        included: list[InclusionRecord] = []
        excluded: list[InclusionRecord] = []
        usage: list[UsageEntry] = []

        domain_id, domain_reason, inference_stats = self._primary_domain(
            task, assistant_request, session.active_domain_id
        )
        domain = None
        if domain_id:
            try:
                domain = self._domains.get_domain(domain_id)
            except KeyError:
                excluded.append(InclusionRecord(item=f"domain:{domain_id}", reason="missing domain", section="domain"))

        priority_scopes = ["global"]
        if domain is not None:
            priority_scopes.append("self" if domain_id == SELF_DOMAIN_ID else f"domain:{domain_id}")
            included.append(InclusionRecord(item=f"domain:{domain_id}", reason=domain_reason, section="domain"))
        if assistant_request and "self" not in priority_scopes:
            priority_scopes.append("self")
            included.append(
                InclusionRecord(
                    item="self",
                    reason="request is about the assistant",
                    section="domain",
                )
            )

        # Scope filtering is deliberately open to every project so relevant
        # knowledge from another project remains retrievable. Priority scopes
        # only break ties and supply a small bonus, never an absolute filter.
        # The reserved self scope stays on demand, matching the assistant's
        # private memory boundary.
        scopes = ["global"]
        for item in self._domains.list_domains():
            if item.id == SELF_DOMAIN_ID:
                continue
            scopes.append(f"domain:{item.id}")
        if assistant_request or domain_id == SELF_DOMAIN_ID:
            scopes.append("self")
        requested = int(self._settings.get("memory.retrieval.k"))
        multiplier = int(self._settings.get("context.retrieval.candidate_multiplier"))
        minimum = int(self._settings.get("context.retrieval.min_candidates"))
        candidate_budget = max(requested, minimum)
        if multiplier > 1:
            candidate_budget = max(candidate_budget, requested * multiplier)
        ranked = self._memory.retrieve(
            task.goal,
            scopes,
            candidate_budget,
            scope_priority=priority_scopes,
        )
        pref_ids = {item.id for item in pref_records}
        mem_ranked = [item for item in ranked if item.record.id not in pref_ids]

        domain_blocks: list[str] = []
        domain_text_parts: list[str] = []
        if domain is not None:
            domain_text_parts.append(f"Domain: {domain.name} ({domain.id})")
            project_summary = self._project_summary(domain.id)
            if project_summary:
                domain_text_parts.append(project_summary)

        budget = int(self._settings.get("context.budgets.domain"))
        chars_per_token = int(self._settings.get("context.chars_per_token"))
        domain_text = "\n".join(domain_text_parts)
        used = estimate_tokens(domain_text, chars_per_token=chars_per_token) if domain_text else 0
        domain_trimmed = False
        for item in mem_ranked:
            block = self._memory_block(item)
            cost = estimate_tokens(block, chars_per_token=chars_per_token)
            if used + cost > budget:
                domain_trimmed = True
                excluded.append(
                    InclusionRecord(
                        item=item.record.id,
                        reason=f"memory would exceed domain budget ({budget} tokens)",
                        section="domain",
                    )
                )
                continue
            domain_blocks.append(block)
            used += cost
            included.append(InclusionRecord(item=item.record.id, reason=item.reason, section="domain"))
            usage.append(
                UsageEntry(
                    ts=now_ts(),
                    memory_id=item.record.id,
                    task_id=task.id,
                    trace_id=trace_id or "",
                    reason=item.reason,
                )
            )
        if domain_blocks:
            domain_text = "\n".join(domain_text_parts + domain_blocks)
        if domain_trimmed:
            excluded.append(
                InclusionRecord(
                    item="domain:overflow",
                    reason="whole memories dropped to honor the domain budget",
                    section="domain",
                )
            )
        elif not domain_blocks and self._has_any_memory():
            # Retrieval is relevance-gated. Keep the omission visible so a
            # trace explains why the section is empty even though memory exists.
            excluded.append(
                InclusionRecord(
                    item="domain:dropped",
                    reason="no memory scored high enough for this request",
                    section="domain",
                )
            )

        pref_text = "\n".join(self._memory_block_for_record(item) for item in pref_records)
        for item in pref_records:
            included.append(InclusionRecord(item=item.id, reason="global preference", section="preferences"))
            usage.append(
                UsageEntry(
                    ts=now_ts(),
                    memory_id=item.id,
                    task_id=task.id,
                    trace_id=trace_id or "",
                    reason="global preference",
                )
            )

        structure_text = self.structure_text(session)
        tz = timezone_name(self._settings)
        now = now_ts()
        task_core = (
            f"Goal: {task.goal}\nState: {task.state}\n"
            f"Constraints: {', '.join(task.constraints) or 'none'}\n"
            f"Dependencies: {', '.join(task.depends_on) or 'none'}\n"
            f"Created: {time_marker(task.created_at, timezone_name=tz, now=now)}"
        )
        if bool(self._settings.get("context.include_time")):
            task_core += f"\n{current_time_block(self._settings)}"
        if any(item.startswith("User reply:") for item in task.constraints):
            task_core += "\nResumed: the latest user message answers your pending question."
        pending_decisions = self._related_pending_decisions(task, domain_id)
        pending_text = self._pending_decision_text(pending_decisions)
        task_text, task_tokens, task_dropped = self._fit_task_with_pending(
            task_core, pending_text, budget=int(self._settings.get("context.budgets.task"))
        )
        if task_dropped:
            excluded.append(
                InclusionRecord(
                    item="task:overflow",
                    reason=task_dropped,
                    section="task",
                )
            )

        history_view = self._history.build()
        history_text = history_view.text
        for note in history_view.included:
            included.append(InclusionRecord(item=note.item, reason=note.reason, section="history"))
        for note in history_view.excluded:
            excluded.append(InclusionRecord(item=note.item, reason=note.reason, section="history"))

        cap_text = self._capability_text(task.domain_id)
        variant = str(self._settings.get("context.prompt_variant"))
        instructions = TRIMMED_INSTRUCTIONS if variant == "trimmed" else BASE_INSTRUCTIONS
        raw = {
            "base_instructions": instructions.strip(),
            "persona": persona_text,
            "preferences": pref_text,
            "structure": structure_text,
            "domain": domain_text,
            "task": task_text,
            "history": history_text,
            "capabilities": cap_text,
        }
        sections: list[ContextSection] = []
        for name in SECTION_ORDER:
            text = raw[name]
            section_budget = int(self._settings.get(f"context.budgets.{name}"))
            tokens = estimate_tokens(text, chars_per_token=chars_per_token)
            if name == "task":
                # ``_fit_task_with_pending`` already protected the pending
                # decision. Do not character-truncate it a second time.
                pass
            elif tokens > section_budget:
                if name in {"base_instructions", "persona"}:
                    raise ValueError(f"{name} exceeds its token budget and must not be truncated")
                if name == "structure":
                    text, tokens, dropped = self._fit_structure(text, section_budget, chars_per_token)
                else:
                    text, tokens, dropped = self._fit(name, text, section_budget)
                excluded.append(
                    InclusionRecord(item=f"{name}:overflow", reason=dropped, section=name)
                )
            sections.append(ContextSection(name=name, text=text, tokens=tokens))

        total_budget = int(self._settings.get("context.budgets.total"))
        total = sum(item.tokens for item in sections)
        drop_order = ["history", "structure", "domain", "preferences", "capabilities"]
        while total > total_budget:
            reduced = False
            for name in drop_order:
                idx = next(i for i, s in enumerate(sections) if s.name == name)
                if sections[idx].tokens <= 0:
                    continue
                sections[idx] = ContextSection(name=name, text="", tokens=0)
                excluded.append(
                    InclusionRecord(item=f"{name}:dropped", reason="total budget", section=name)
                )
                reduced = True
                total = sum(item.tokens for item in sections)
                break
            if not reduced:
                # The remaining overflow is safety instructions plus persona and
                # the task block. Safety instructions and pending decisions must
                # survive, so trim the persona instead of raising when possible.
                trimmed = self._trim_persona_to_budget(sections, total_budget, chars_per_token)
                if trimmed is None:
                    raise ValueError("context exceeds total budget after dropping optional sections")
                excluded.append(
                    InclusionRecord(
                        item="persona:overflow",
                        reason=trimmed,
                        section="persona",
                    )
                )
                total = sum(item.tokens for item in sections)

        body = "\n\n".join(f"## {item.name}\n{item.text}" for item in sections if item.text)
        # Chat templates commonly require a user turn; keep the context in the
        # system message. Use the latest user text so a resumed task sees the
        # reply that unblocked it, not only the original task goal.
        latest = self._sessions.list_interactions(limit=1)
        user_text = latest[-1].user_text if latest else task.goal
        messages = [
            Message(role="system", content=body),
            Message(role="user", content=user_text),
        ]
        if usage:
            self._memory.record_usage(usage)
        return ContextPackage(
            sections=sections,
            messages=messages,
            total_tokens=sum(item.tokens for item in sections),
            included=included,
            excluded=excluded,
            stats={
                "history": history_view.stats,
                "project": {
                    "primary_domain_id": domain_id,
                    "primary_domain_name": domain.name if domain is not None else None,
                    "reason": domain_reason,
                    "inferred": inference_stats.get("inferred", False),
                    "candidates": inference_stats.get("candidates", []),
                },
                "retrieval": {
                    "query": task.goal,
                    "scopes": priority_scopes,
                    "candidates": len(ranked),
                    "selected": len(domain_blocks),
                    "preferences": len(pref_records),
                },
                "budget": {
                    "total": sum(item.tokens for item in sections),
                    "limit": total_budget,
                    "sections": {item.name: item.tokens for item in sections},
                },
            },
        )

    def _trim_persona_to_budget(
        self, sections: list[ContextSection], total_budget: int, chars_per_token: int
    ) -> str | None:
        """Shrink only the persona so safety instructions fit the total budget.

        Returns the exclusion reason, or ``None`` when even an empty persona
        cannot make the package fit (the caller then raises).
        """
        others = sum(item.tokens for item in sections if item.name != "persona")
        available = total_budget - others
        if available < 0:
            return None
        idx = next(i for i, s in enumerate(sections) if s.name == "persona")
        persona = sections[idx]
        if available == 0:
            if persona.tokens <= 0:
                return None
            sections[idx] = ContextSection(name="persona", text="", tokens=0)
            return "persona dropped so safety instructions fit the total budget"
        keep = available * chars_per_token
        text = persona.text[:keep].rstrip()
        sections[idx] = ContextSection(
            name="persona", text=text, tokens=estimate_tokens(text, chars_per_token=chars_per_token)
        )
        return (
            "persona shortened so safety instructions fit the total budget "
            f"({persona.tokens} to {sections[idx].tokens} tokens)"
        )

    def _primary_domain(
        self, task: Task, assistant_request: bool, active_domain_id: str | None
    ) -> tuple[str | None, str, dict[str, object]]:
        """Return the project used to order context, not a persisted association."""
        if task.domain_id:
            return task.domain_id, "task domain", {"inferred": False}
        if self._inference is None:
            return None, "no project", {"inferred": False}
        try:
            match = self._inference.infer(task.goal, active_domain_id=active_domain_id)
        except Exception:
            return None, "inference unavailable", {"inferred": False}
        if match.domain_id is None or (assistant_request and match.domain_id == SELF_DOMAIN_ID):
            return None, "no project", {"inferred": False}
        # Context ordering follows the same rule as task routing: a confident
        # match, or the user's explicit active selection as a fallback. A weak
        # best-match with no confidence must not relabel the prompt.
        if not (match.confident or match.domain_id == active_domain_id):
            return None, "no project", {"inferred": False}
        reason = f"inferred project ({match.reason})" if match.reason else "inferred project"
        return (
            match.domain_id,
            reason,
            {
                "inferred": True,
                "confidence": match.score,
                "candidates": [match.name] if match.name else [],
            },
        )

    def _project_summary(self, domain_id: str) -> str:
        if not bool(self._settings.get("context.project.include_summary")):
            return ""
        if self._inference is None:
            return ""
        try:
            limit = int(self._settings.get("context.project.max_tasks"))
            return self._inference.summary(domain_id, limit=limit)
        except Exception:
            return ""

    def _related_pending_decisions(self, task: Task, domain_id: str | None) -> list[Decision]:
        if self._decisions is None:
            return []
        limit = int(self._settings.get("context.pending_decisions.max"))
        if limit <= 0:
            return []
        include_project = bool(self._settings.get("context.pending_decisions.include_project"))
        try:
            pending = list(self._decisions.pending())
        except Exception:
            return []
        related: list[Decision] = []
        for item in pending:
            linked = task.id in item.task_ids
            project = bool(
                include_project
                and domain_id
                and (
                    item.domain_id == domain_id
                    or (item.scope or {}).get("domain_id") == domain_id
                )
            )
            if linked or project:
                related.append(item)
            if len(related) >= limit:
                break
        return related

    @staticmethod
    def _pending_decision_text(pending: list[Decision]) -> str:
        if not pending:
            return ""
        lines = [f"- {item.id}: {item.summary} (risk: {item.risk})" for item in pending]
        return "Pending decisions for this task or project:\n" + "\n".join(lines)

    def _fit_task_with_pending(
        self, core: str, pending: str, *, budget: int
    ) -> tuple[str, int, str]:
        chars = int(self._settings.get("context.chars_per_token"))
        pending_tokens = estimate_tokens(pending, chars_per_token=chars) if pending else 0
        if pending_tokens >= budget and pending:
            # The decision is safety-relevant: keep the whole (short) summary
            # line rather than cutting the text that explains it.
            text = pending
            return text, estimate_tokens(text, chars_per_token=chars), "task details dropped to preserve pending decision"
        available = max(0, budget - pending_tokens)
        core_text = core
        dropped = ""
        if estimate_tokens(core_text, chars_per_token=chars) > available:
            keep = max(0, available * chars)
            core_text = core_text[:keep].rstrip()
            dropped = f"shortened task to budget {available} tokens to preserve pending decision"
        text = core_text + (("\n" + pending) if pending else "")
        return text, estimate_tokens(text, chars_per_token=chars), dropped

    def _memory_block(self, item: RankedMemory) -> str:
        record = item.record
        return (
            f'<memory id="{record.id}" kind="{record.kind}" '
            f'type="{record.type}" scope="{record.scope}" '
            f'lifecycle="{self._lifecycle(record)}" '
            f'{self._memory_time(record.updated_at)} origin="{record.origin}">\n'
            f"{record.content}\n</memory>"
        )

    def _memory_block_for_record(self, record) -> str:
        return (
            f'<memory id="{record.id}" kind="{record.kind}" type="preference" '
            f'scope="global" lifecycle="{self._lifecycle(record)}" '
            f'{self._memory_time(record.updated_at)} origin="{record.origin}">\n'
            f"{record.content}\n</memory>"
        )

    def _lifecycle(self, record) -> str:
        try:
            return self._memory.lifecycle(record)
        except Exception:
            return ""

    def _has_any_memory(self) -> bool:
        try:
            return bool(self._memory.list(status="active"))
        except Exception:
            return False

    def structure_text(self, session: Session) -> str:
        """Render the compact domain directory.

        The context section and the task planner both use this exact text so
        the model sees the same structure in both prompts.
        """
        return "\n".join(self._structure_lines(session))

    def _structure_lines(self, session: Session) -> list[str]:
        domains = self._domains.list_domains()
        active_id = session.active_domain_id
        active_name: str | None = None
        if active_id:
            try:
                active_name = self._domains.get_domain(active_id).name
            except KeyError:
                active_id = None
        lines = [
            f"Active domain: {active_name} ({active_id})" if active_id else "Active domain: none"
        ]
        lines.append("Domains:")
        for domain in domains:
            lines.append(f"- {domain.id} | {domain.name}")
        pending = [item for item in self._domains.list_suggestions() if item.status == "pending"]
        lines.append("Pending suggestions:")
        if not pending:
            lines.append("- none")
        for item in pending:
            lines.append(f"- {item.id} | {item.draft.name}")
        return lines

    def _fit_structure(self, text: str, budget: int, chars_per_token: int) -> tuple[str, int, str]:
        """Drop whole directory blocks (active, domains, suggestions)."""
        lines = text.splitlines()
        if not lines:
            return "", 0, ""
        blocks: list[list[str]] = [[lines[0]]]
        for line in lines[1:]:
            if line.startswith("- "):
                blocks[-1].append(line)
            else:
                blocks.append([line])
        dropped: list[str] = []
        while blocks and estimate_tokens(
            "\n".join("\n".join(block) for block in blocks),
            chars_per_token=chars_per_token,
        ) > budget:
            removed = blocks.pop()
            dropped.append(removed[0].rstrip(":"))
        fitted = "\n".join("\n".join(block) for block in blocks)
        reason = (
            "dropped whole structure entries: " + ", ".join(reversed(dropped))
            if dropped
            else ""
        )
        return fitted, estimate_tokens(fitted, chars_per_token=chars_per_token), reason

    def _memory_time(self, value: str) -> str:
        tz = timezone_name(self._settings)
        marker = time_marker(value, timezone_name=tz, now=now_ts())
        age = gap_between(value, now_ts())
        notable = float(self._settings.get("context.time.gap_hours")) * 3600
        if age >= notable:
            return f'updated="{marker}" age="{human_gap(age)}"'
        return f'updated="{marker}"'

    def _persona_block(self, persona) -> str:
        principles = "\n".join(f"- {p}" for p in persona.operating_principles)
        return (
            f"Name: {persona.name}\nPurpose: {persona.purpose}\n"
            f"Style: {persona.communication_style}\nPrinciples:\n{principles}"
        )

    def _is_assistant_request(self, goal: str) -> bool:
        return matches_keywords(
            goal, str(self._settings.get("context.self_scope_keywords") or "")
        )

    def _capability_text(self, domain_id: str | None) -> str:
        enabled = set()
        if domain_id:
            try:
                enabled = set(self._domains.enablement(domain_id))
            except KeyError:
                enabled = set()
        else:
            raw = str(self._settings.get("capabilities.default_enabled") or "")
            enabled = {item.strip() for item in raw.split(",") if item.strip()}
        lines = []
        for manifest in self._capabilities.list_manifests():
            if manifest.name not in enabled:
                continue
            lines.append(f"- {manifest.name}: {manifest.purpose}")
            if not manifest.operations:
                lines.append("  operations: instructions only")
            for op in manifest.operations:
                args = ", ".join(self._argument_text(op, key, kind) for key, kind in op.inputs.items()) or "none"
                required = ", ".join(op.required) or "none"
                lines.append(
                    f"  - {op.name} ({op.permission}): {op.description} "
                    f"args: {args}; required: {required}"
                )
            if manifest.skill_body:
                lines.append(manifest.skill_body)
        return "\n".join(lines)

    def _argument_text(self, op, key: str, kind: str) -> str:
        text = f"{key}:{kind}"
        allowed = op.enums.get(key)
        if allowed and bool(self._settings.get("capabilities.disclose_enum_values")):
            text += " (one of " + "|".join(allowed) + ")"
        if key not in op.required:
            text += " [optional"
            if key in op.defaults:
                text += f", default {op.defaults[key]}"
            text += "]"
        return text

    def _fit(self, name: str, text: str, budget: int) -> tuple[str, int, str]:
        # shorten by cutting trailing characters to the budget
        chars = int(self._settings.get("context.chars_per_token"))
        keep = max(0, budget * chars)
        trimmed = text[:keep]
        return trimmed, estimate_tokens(trimmed, chars_per_token=chars), f"shortened {name} to budget {budget}"

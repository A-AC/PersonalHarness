from __future__ import annotations

from harness.constants import MemoryType, SELF_DOMAIN_ID
from harness.contracts.capabilities import CapabilityExecutor
from harness.contracts.context import ContextPackage, ContextSection, InclusionRecord
from harness.contracts.domains import DomainRegistry
from harness.contracts.history import HistoryService
from harness.contracts.memory import MemoryManager, UsageEntry
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
    ):
        self._settings = settings
        self._memory = memory
        self._profile = profile
        self._domains = domains
        self._capabilities = capabilities
        self._sessions = sessions
        self._history = history

    def build(self, task: Task, session: Session, *, trace_id: str | None = None) -> ContextPackage:
        persona = self._profile.current()
        persona_text = self._persona_block(persona)
        pref_records = self._memory.list(scope="global", status="active", type=MemoryType.PREFERENCE)
        assistant_request = self._is_assistant_request(task.goal)
        scopes = ["global"]
        domain_text_parts: list[str] = []
        included: list[InclusionRecord] = []
        excluded: list[InclusionRecord] = []
        project = None
        domain_id = task.domain_id
        if task.project_id:
            try:
                project = self._domains.get_project(task.project_id)
                if domain_id is None:
                    domain_id = project.domain_id
            except KeyError:
                excluded.append(InclusionRecord(item=f"project:{task.project_id}", reason="missing project", section="domain"))
        if domain_id:
            scopes.append(f"domain:{domain_id}" if domain_id != SELF_DOMAIN_ID else "self")
            try:
                domain = self._domains.get_domain(domain_id)
                domain_text_parts.append(f"Domain: {domain.name} ({domain.id})")
                included.append(InclusionRecord(item=f"domain:{domain.id}", reason="task domain", section="domain"))
            except KeyError:
                excluded.append(InclusionRecord(item=f"domain:{domain_id}", reason="missing domain", section="domain"))
        if project is not None:
            scopes.append(f"project:{project.id}")
            domain_text_parts.append(f"Project: {project.name} ({project.id})")
            included.append(InclusionRecord(item=f"project:{project.id}", reason="task project", section="domain"))
            if project.workspace_id:
                try:
                    workspace = self._domains.get_workspace(project.workspace_id)
                    domain_text_parts.append(f"Workspace: {workspace.name} ({workspace.id})")
                    included.append(
                        InclusionRecord(item=f"workspace:{workspace.id}", reason="project workspace", section="domain")
                    )
                except KeyError:
                    excluded.append(
                        InclusionRecord(item=f"workspace:{project.workspace_id}", reason="missing workspace", section="domain")
                    )
        if assistant_request and "self" not in scopes:
            scopes.append("self")
            included.append(InclusionRecord(item="self", reason="request is about the assistant", section="domain"))
        domain_memories = self._memory.retrieve(task.goal, scopes, int(self._settings.get("memory.retrieval.k")))
        pref_ids = {item.id for item in pref_records}
        mem_blocks = []
        usage: list[UsageEntry] = []
        for ranked in domain_memories:
            if ranked.record.id in pref_ids:
                continue
            block = (
                f'<memory id="{ranked.record.id}" kind="{ranked.record.kind}" '
                f'type="{ranked.record.type}" scope="{ranked.record.scope}" '
                f'{self._memory_time(ranked.record.updated_at)} origin="{ranked.record.origin}">\n'
                f"{ranked.record.content}\n</memory>"
            )
            mem_blocks.append(block)
            included.append(InclusionRecord(item=ranked.record.id, reason=ranked.reason, section="domain"))
            usage.append(
                UsageEntry(
                    ts=now_ts(),
                    memory_id=ranked.record.id,
                    task_id=task.id,
                    trace_id=trace_id or "",
                    reason=ranked.reason,
                )
            )
        pref_text = "\n".join(
            f'<memory id="{item.id}" kind="{item.kind}" type="preference" scope="global" '
            f'{self._memory_time(item.updated_at)} origin="{item.origin}">\n{item.content}\n</memory>'
            for item in pref_records
        )
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
        domain_text = "\n".join(domain_text_parts + mem_blocks)
        structure_text = self.structure_text(session)
        tz = timezone_name(self._settings)
        now = now_ts()
        task_text = (
            f"Goal: {task.goal}\nState: {task.state}\n"
            f"Constraints: {', '.join(task.constraints) or 'none'}\n"
            f"Dependencies: {', '.join(task.depends_on) or 'none'}\n"
            f"Created: {time_marker(task.created_at, timezone_name=tz, now=now)}"
        )
        if bool(self._settings.get("context.include_time")):
            task_text += f"\n{current_time_block(self._settings)}"
        if any(item.startswith("User reply:") for item in task.constraints):
            task_text += "\nResumed: the latest user message answers your pending question."
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
        chars_per_token = int(self._settings.get("context.chars_per_token"))
        for name in SECTION_ORDER:
            text = raw[name]
            budget = int(self._settings.get(f"context.budgets.{name}"))
            tokens = estimate_tokens(text, chars_per_token=chars_per_token)
            if tokens > budget:
                if name in {"base_instructions", "persona"}:
                    raise ValueError(f"{name} exceeds its token budget and must not be truncated")
                if name == "structure":
                    # The directory is a list of entries. Drop whole entries so
                    # a half-rendered domain or project line never reaches the
                    # prompt.
                    text, tokens, dropped = self._fit_structure(text, budget, chars_per_token)
                else:
                    text, tokens, dropped = self._fit(name, text, budget)
                excluded.append(InclusionRecord(item=f"{name}:overflow", reason=dropped, section=name))
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
                excluded.append(InclusionRecord(item=f"{name}:dropped", reason="total budget", section=name))
                reduced = True
                total = sum(item.tokens for item in sections)
                break
            if not reduced:
                raise ValueError("context exceeds total budget after dropping optional sections")
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
            stats={"history": history_view.stats},
        )

    def structure_text(self, session: Session) -> str:
        """Render the compact domain/project directory.

        The context section and the task planner both use this exact text so
        the model sees the same structure in both prompts.
        """
        return "\n".join(self._structure_lines(session))

    def _structure_lines(self, session: Session) -> list[str]:
        domains = self._domains.list_domains()
        projects = self._domains.list_projects()
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
        counts: dict[str, int] = {}
        for project in projects:
            counts[project.domain_id] = counts.get(project.domain_id, 0) + 1
        domain_names = {domain.id: domain.name for domain in domains}
        lines.append("Domains:")
        for domain in domains:
            lines.append(f"- {domain.id} | {domain.name} | projects: {counts.get(domain.id, 0)}")
        lines.append("Projects:")
        if not projects:
            lines.append("- none")
        for project in projects:
            lines.append(
                f"- {project.id} | {project.name} | domain: "
                f"{domain_names.get(project.domain_id, project.domain_id)}"
            )
        pending = [item for item in self._domains.list_suggestions() if item.status == "pending"]
        lines.append("Pending suggestions:")
        if not pending:
            lines.append("- none")
        for item in pending:
            lines.append(f"- {item.id} | {item.draft.kind} | {item.draft.name}")
        return lines

    def _fit_structure(self, text: str, budget: int, chars_per_token: int) -> tuple[str, int, str]:
        """Drop whole directory blocks (active, domains, projects, suggestions)."""
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

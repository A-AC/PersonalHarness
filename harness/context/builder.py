from __future__ import annotations

from harness.constants import MemoryType, SELF_DOMAIN_ID
from harness.contracts.capabilities import CapabilityExecutor
from harness.contracts.context import ContextPackage, ContextSection, InclusionRecord
from harness.contracts.domains import DomainRegistry
from harness.contracts.memory import MemoryManager, UsageEntry
from harness.contracts.model import Message
from harness.contracts.profile import SelfProfile
from harness.contracts.session import Session, SessionManager
from harness.contracts.settings import SettingsService
from harness.contracts.tasks import Task
from harness.prompts import BASE_INSTRUCTIONS
from harness.storage.atomic import now_ts
from harness.text import estimate_tokens


SECTION_ORDER = [
    "base_instructions",
    "persona",
    "preferences",
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
    ):
        self._settings = settings
        self._memory = memory
        self._profile = profile
        self._domains = domains
        self._capabilities = capabilities
        self._sessions = sessions

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
                f'<memory id="{ranked.record.id}" type="{ranked.record.type}" scope="{ranked.record.scope}" '
                f'updated="{ranked.record.updated_at}" origin="{ranked.record.origin}">\n'
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
            f'<memory id="{item.id}" type="preference" scope="global" updated="{item.updated_at}" origin="{item.origin}">\n{item.content}\n</memory>'
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
        task_text = (
            f"Goal: {task.goal}\nState: {task.state}\n"
            f"Constraints: {', '.join(task.constraints) or 'none'}\n"
            f"Dependencies: {', '.join(task.depends_on) or 'none'}"
        )
        history_msgs = self._sessions.recent_history()
        history_text = "\n".join(f"{m.role}: {m.content}" for m in history_msgs)
        cap_text = self._capability_text(task.domain_id)
        raw = {
            "base_instructions": BASE_INSTRUCTIONS.strip(),
            "persona": persona_text,
            "preferences": pref_text,
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
                text, tokens, dropped = self._fit(name, text, budget)
                excluded.append(InclusionRecord(item=f"{name}:overflow", reason=dropped, section=name))
            sections.append(ContextSection(name=name, text=text, tokens=tokens))
        total_budget = int(self._settings.get("context.budgets.total"))
        total = sum(item.tokens for item in sections)
        drop_order = ["history", "domain", "preferences", "capabilities"]
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
        # system message and make the task's goal the user query.
        messages = [
            Message(role="system", content=body),
            Message(role="user", content=task.goal),
        ]
        if usage:
            self._memory.record_usage(usage)
        return ContextPackage(
            sections=sections,
            messages=messages,
            total_tokens=sum(item.tokens for item in sections),
            included=included,
            excluded=excluded,
        )

    def _persona_block(self, persona) -> str:
        principles = "\n".join(f"- {p}" for p in persona.operating_principles)
        return (
            f"Name: {persona.name}\nPurpose: {persona.purpose}\n"
            f"Style: {persona.communication_style}\nPrinciples:\n{principles}"
        )

    def _is_assistant_request(self, goal: str) -> bool:
        lowered = goal.lower()
        raw = str(self._settings.get("context.self_scope_keywords") or "")
        keywords = [item.strip().lower() for item in raw.split(",") if item.strip()]
        return any(keyword in lowered for keyword in keywords)

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
            ops = ", ".join(f"{op.name}({op.permission})" for op in manifest.operations) or "instructions only"
            lines.append(f"- {manifest.name}: {manifest.purpose} operations: {ops}")
            if manifest.skill_body:
                lines.append(manifest.skill_body)
        return "\n".join(lines)

    def _fit(self, name: str, text: str, budget: int) -> tuple[str, int, str]:
        # shorten by cutting trailing characters to the budget
        chars = int(self._settings.get("context.chars_per_token"))
        keep = max(0, budget * chars)
        trimmed = text[:keep]
        return trimmed, estimate_tokens(trimmed, chars_per_token=chars), f"shortened {name} to budget {budget}"

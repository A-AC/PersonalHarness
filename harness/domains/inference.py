"""Temporary project association from a request, history, tasks, and memory.

The active domain is deliberately only one weak signal. Inference never creates
a domain, never changes the session selection, and never moves a task or a
memory. Callers may use the returned match to choose a scope for a *new* task
or to order context retrieval; the persisted association remains explicit.
"""

from __future__ import annotations

from harness.constants import SELF_DOMAIN_ID
from harness.contracts.domains import Domain, DomainInference, DomainMatch
from harness.contracts.memory import MemoryManager
from harness.contracts.session import SessionManager
from harness.contracts.settings import SettingsService
from harness.contracts.tasks import TaskManager
from harness.text import STOPWORDS, name_overlap, word_tokens


class DefaultDomainInference:
    """Deterministic, explainable project inference.

    Signals are additive: an explicit name match, a relevant memory outside the
    active scope, an already-open task in that project, recent conversation
    linked to that project, and finally the active-domain hint. A confident
    match must clear a configured score and beat the runner-up by a margin.
    Otherwise the caller sees ``confident=False`` and can fall back to the
    active domain or simply use no project scope.
    """

    def __init__(
        self,
        settings: SettingsService,
        domains,
        tasks: TaskManager,
        memory: MemoryManager,
        sessions: SessionManager,
    ):
        self._settings = settings
        self._domains = domains
        self._tasks = tasks
        self._memory = memory
        self._sessions = sessions

    def infer(
        self,
        query: str,
        *,
        active_domain_id: str | None = None,
    ) -> DomainMatch:
        matches = self.infer_many(query, active_domain_id=active_domain_id, limit=2)
        if not matches:
            return DomainMatch(domain_id=None, name="", score=0.0, reason="no project matched")
        best = matches[0]
        if not best.confident:
            # Preserve the user's explicit working selection when the request
            # does not contain enough evidence to choose a different project.
            active = self._active_match(matches, active_domain_id)
            if active is not None:
                return active
        return best

    def infer_many(
        self,
        query: str,
        *,
        active_domain_id: str | None = None,
        limit: int | None = None,
    ) -> list[DomainMatch]:
        if not bool(self._settings.get("domain.inference.enabled")):
            return []
        if active_domain_id is None:
            active_domain_id = self._sessions.active_domain()
        domains = [
            item
            for item in self._domains.list_domains()
            if item.id != SELF_DOMAIN_ID and not item.archived
        ]
        if not domains:
            return []

        query_tokens = {item for item in word_tokens(query) if item not in STOPWORDS}
        memory_scores = self._memory_scores(query, domains)
        task_scores = self._task_scores(query_tokens, domains)
        history_scores = self._history_scores(query_tokens, domains)

        name_weight = float(self._settings.get("domain.inference.name_weight"))
        memory_weight = float(self._settings.get("domain.inference.memory_weight"))
        task_weight = float(self._settings.get("domain.inference.task_weight"))
        history_weight = float(self._settings.get("domain.inference.history_weight"))
        active_bonus = float(self._settings.get("domain.inference.active_bonus"))

        matches: list[DomainMatch] = []
        for domain in domains:
            name_score = self._name_score(query_tokens, domain)
            memory_score = memory_scores.get(domain.id, 0.0)
            task_score = task_scores.get(domain.id, 0.0)
            history_score = history_scores.get(domain.id, 0.0)
            active = active_bonus if domain.id == active_domain_id else 0.0
            score = (
                name_score * name_weight
                + memory_score * memory_weight
                + task_score * task_weight
                + history_score * history_weight
                + active
            )
            evidence = {
                "name": name_score,
                "memory": memory_score,
                "task": task_score,
                "history": history_score,
                "active": active,
            }
            reasons = [key for key, value in evidence.items() if value > 0]
            matches.append(
                DomainMatch(
                    domain_id=domain.id,
                    name=domain.name,
                    score=score,
                    reason=", ".join(reasons) or "no signal",
                    evidence=evidence,
                )
            )

        matches.sort(key=lambda item: (item.score, item.name.lower()), reverse=True)
        threshold = float(self._settings.get("domain.inference.confident_score"))
        margin = float(self._settings.get("domain.inference.ambiguous_margin"))
        if matches:
            top = matches[0]
            runner_up = matches[1].score if len(matches) > 1 else 0.0
            top.confident = top.score >= threshold and (top.score - runner_up) >= margin
            if len(matches) > 1 and (top.score - runner_up) < margin and top.score > 0:
                top.ambiguous = True
                top.candidates = [
                    item.name for item in matches if (top.score - item.score) < margin
                ][:4]
        if limit is not None:
            return matches[: max(0, limit)]
        return matches

    def summary(self, domain_id: str, *, limit: int = 4) -> str:
        """Render a short project brief without changing any stored state."""
        try:
            domain = self._domains.get_domain(domain_id)
        except KeyError:
            return ""
        task_lines: list[str] = []
        active_states = {"pending", "ready", "running", "blocked"}
        for task in self._tasks.list():
            if task.domain_id != domain_id or task.state not in active_states:
                continue
            task_lines.append(f"- {task.goal} ({task.state})")
            if len(task_lines) >= max(0, int(limit)):
                break
        lines = [f"Project: {domain.name} ({domain.id})"]
        if task_lines:
            lines.append("Open work:")
            lines.extend(task_lines)
        return "\n".join(lines)

    def _active_match(
        self, matches: list[DomainMatch], active_domain_id: str | None
    ) -> DomainMatch | None:
        if not active_domain_id:
            return None
        for item in matches:
            if item.domain_id == active_domain_id:
                if item.score <= 0:
                    return None
                item.confident = False
                return item
        return None

    def _memory_scores(self, query: str, domains: list[Domain]) -> dict[str, float]:
        if not query.strip():
            return {}
        budget = int(self._settings.get("domain.inference.memory_candidates"))
        ranked = self._memory.retrieve(query, [], budget, scope_priority=[])
        scores: dict[str, float] = {}
        for item in ranked:
            scope = item.record.scope
            if not scope.startswith("domain:"):
                continue
            domain_id = scope.split(":", 1)[1]
            # Retrieval scores are additive; cap the evidence contribution so
            # one long memory cannot outweigh a clear name match indefinitely.
            value = min(1.0, max(0.0, item.score))
            scores[domain_id] = max(scores.get(domain_id, 0.0), value)
        return scores

    def _task_scores(
        self, query_tokens: set[str], domains: list[Domain]
    ) -> dict[str, float]:
        if not query_tokens:
            return {}
        scores: dict[str, float] = {}
        active_states = {"pending", "ready", "running", "blocked"}
        for task in self._tasks.list():
            if not task.domain_id or task.state not in active_states:
                continue
            overlap = self._overlap(query_tokens, task.goal)
            if overlap <= 0:
                continue
            scores[task.domain_id] = max(scores.get(task.domain_id, 0.0), overlap)
        return {
            domain.id: scores.get(domain.id, 0.0)
            for domain in domains
            if domain.id in scores
        }

    def _history_scores(
        self, query_tokens: set[str], domains: list[Domain]
    ) -> dict[str, float]:
        if not query_tokens:
            return {}
        window = int(self._settings.get("domain.inference.history_interactions"))
        if window <= 0:
            return {}
        interactions = list(self._sessions.list_interactions(limit=window))
        if not interactions:
            return {}
        task_domains = {
            task.id: task.domain_id for task in self._tasks.list() if task.domain_id
        }
        scores: dict[str, float] = {}
        for interaction in interactions:
            overlap = self._overlap(query_tokens, interaction.user_text)
            if overlap <= 0:
                continue
            for task_id in interaction.task_ids or []:
                domain_id = task_domains.get(task_id)
                if domain_id:
                    scores[domain_id] = max(scores.get(domain_id, 0.0), overlap)
        return scores

    @staticmethod
    def _name_score(query_tokens: set[str], domain: Domain) -> float:
        name_tokens = {item for item in word_tokens(domain.name) if item not in STOPWORDS}
        if not name_tokens or not query_tokens:
            return 0.0
        if name_overlap(domain.name, " ".join(sorted(query_tokens))) >= 1.0:
            return 1.0
        return len(name_tokens & query_tokens) / len(name_tokens)

    @staticmethod
    def _overlap(query_tokens: set[str], text: str) -> float:
        text_tokens = {item for item in word_tokens(text) if item not in STOPWORDS}
        if not query_tokens or not text_tokens:
            return 0.0
        return len(query_tokens & text_tokens) / len(query_tokens)


__all__ = ["DefaultDomainInference"]

"""Deterministic, usage-based domain suggestions.

The manager looks for repeated vocabulary in recent General work. It never
creates a domain and never calls a model: a suggestion is only a durable
proposal that the user can open, edit, dismiss, or explicitly create.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Any

from . import util
from .store import GENERAL_DOMAIN


# A scan is deliberately bounded. The window is capped at roughly ten months
# and no more than 300 recent General tasks are considered in one pass.
MAX_WINDOW_DAYS = 300
MAX_TASKS_PER_SCAN = 300
MAX_CLUSTER_TERMS = 5
MAX_NAME_TERMS = 3
MAX_NAME_CHARS = 48

# Words that describe the request rather than the subject area. They are
# filtered after stemming, so plural/gerund forms are covered as well.
GENERIC_TERMS = frozenset({
    "again", "also", "answer", "anything", "check", "complete", "continue",
    "create", "current", "default", "domain", "finish", "follow", "general",
    "handle", "help", "later", "manage", "misc", "miscellaneous", "new",
    "note", "old", "other", "question", "recent", "reminder", "request",
    "review", "something", "stuff", "task", "thing", "today", "tomorrow",
    "update", "work",
})

_ACRONYMS = {
    "api": "API",
    "cli": "CLI",
    "cpu": "CPU",
    "dns": "DNS",
    "gpu": "GPU",
    "html": "HTML",
    "http": "HTTP",
    "https": "HTTPS",
    "llm": "LLM",
    "ml": "ML",
    "pdf": "PDF",
    "sql": "SQL",
    "ssh": "SSH",
    "ui": "UI",
    "url": "URL",
    "xml": "XML",
}


def _meaningful_terms(text: str) -> list[str]:
    """Stemmed subject terms with short and generic vocabulary removed."""
    out: list[str] = []
    for term in util.stemmed_keywords(str(text or "")):
        if len(term) < 3 or term.isdigit() or term in GENERIC_TERMS:
            continue
        if term not in out:
            out.append(term)
    return out


def _title_word(term: str) -> str:
    cleaned = str(term or "").strip().replace("-", " ").replace("_", " ")
    if not cleaned:
        return ""
    if cleaned.lower() in _ACRONYMS:
        return _ACRONYMS[cleaned.lower()]
    return " ".join(
        part[:1].upper() + part[1:].lower() if part else ""
        for part in cleaned.split()
    )


def proposed_name(terms: list[str]) -> str:
    """Build the deterministic display/proposal name from shared terms."""
    words = [
        word for term in list(terms or [])[:MAX_NAME_TERMS]
        if (word := _title_word(str(term)))
    ]
    name = " ".join(words)
    if len(name) <= MAX_NAME_CHARS:
        return name
    clipped = name[:MAX_NAME_CHARS]
    if " " in clipped:
        clipped = clipped[:clipped.rfind(" ")]
    return clipped.rstrip(" -")


def _name_key(name: str) -> str:
    """Comparable form for names (case-insensitive and stemmed)."""
    return " ".join(util.stemmed_keywords(str(name or "")))


def _decoded_list(value: Any) -> list[str]:
    decoded = util.loads(str(value or ""), [])
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded]


class DomainSuggestions:
    """Persistent proposals derived from repeated General-task vocabulary."""

    def __init__(self, app):
        self.db = app.db
        self.config = app.config
        self.tasks = app.tasks
        self.domains = app.domains
        self.events = app.events
        self.messages = app.messages

    # -- reads and state transitions -------------------------------------

    @staticmethod
    def _to_public(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        out["terms"] = _decoded_list(out.get("terms"))
        out["task_ids"] = _decoded_list(out.get("task_ids"))
        out["name"] = proposed_name(out["terms"])
        out["task_count"] = len(out["task_ids"])
        return out

    def get(self, suggestion_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM domain_suggestions WHERE id=?", (suggestion_id,)
        )
        if row is None:
            return None
        return self._to_public(dict(row))

    def list(
        self, status: str = "open", limit: int = 100
    ) -> list[dict[str, Any]]:
        """List suggestions newest first with JSON columns decoded."""
        if status not in ("open", "created", "dismissed", "all", None):
            raise ValueError("unknown domain suggestion status")
        bounded = max(0, int(limit))
        if bounded == 0:
            return []
        sql = "SELECT * FROM domain_suggestions"
        params: list[Any] = []
        if status not in ("all", None):
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(bounded)
        return [
            item for item in map(
                self._to_public, self.db.query(sql, tuple(params))
            )
            if item is not None
        ]

    def dismiss(self, suggestion_id: str) -> dict[str, Any]:
        """Dismiss a suggestion. Repeating the action is harmless."""
        current = self.get(suggestion_id)
        if current is None:
            raise ValueError(f"no such domain suggestion: {suggestion_id}")
        if current.get("status") == "dismissed":
            return current
        self.db.execute(
            "UPDATE domain_suggestions SET status='dismissed' WHERE id=?",
            (suggestion_id,),
        )
        self.events.record(
            "domain.suggestion.dismissed",
            data={
                "id": current["id"],
                "name": current.get("name") or "",
                "terms": list(current.get("terms") or []),
            },
        )
        return self.get(suggestion_id)  # type: ignore[return-value]

    def mark_created(self, suggestion_id: str) -> dict[str, Any]:
        """Mark a suggestion as acted on. Repeating the action is harmless."""
        current = self.get(suggestion_id)
        if current is None:
            raise ValueError(f"no such domain suggestion: {suggestion_id}")
        if current.get("status") == "created":
            return current
        self.db.execute(
            "UPDATE domain_suggestions SET status='created' WHERE id=?",
            (suggestion_id,),
        )
        self.events.record(
            "domain.suggestion.created",
            data={
                "id": current["id"],
                "name": current.get("name") or "",
                "terms": list(current.get("terms") or []),
            },
        )
        return self.get(suggestion_id)  # type: ignore[return-value]

    # -- deterministic scan ----------------------------------------------

    def scan(self, now: dt.datetime | str | None = None) -> dict[str, Any]:
        """Find one repeated General-task vocabulary cluster, if any.

        The pass is model-free and writes at most one open suggestion plus one
        visible note. Existing suggestions (including dismissed ones) are
        permanent memory: a cluster that overlaps any of them is skipped.
        """
        if not bool(self.config.get("domains.suggestions.enabled", True)):
            return {"status": "disabled"}

        now_dt = self._now(now)
        window_days = self._window_days()
        cutoff = now_dt - dt.timedelta(days=window_days)
        rows = self.db.query(
            "SELECT id, objective, created_at, domain_id, role FROM tasks "
            "WHERE (domain_id IS NULL OR domain_id=?) "
            "AND COALESCE(role, 'user') != 'subagent' "
            "AND created_at>=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (GENERAL_DOMAIN, util.iso(cutoff), MAX_TASKS_PER_SCAN),
        )
        # Keep the bounded corpus recent while restoring chronological order
        # for deterministic first-seen tie breaks.
        task_rows = [dict(row) for row in reversed(rows)]
        examined = len(task_rows)
        min_tasks = self._min_tasks()
        if examined < min_tasks:
            return {
                "status": "completed", "created": [], "examined": examined,
                "clusters": 0,
            }

        covered = self._active_domain_terms()
        term_tasks: dict[str, list[str]] = {}
        task_terms: dict[str, set[str]] = {}
        term_first_seen: dict[str, int] = {}
        surface_by_stem: dict[str, str] = {}
        for task in task_rows:
            objective = str(task.get("objective") or "")
            for token in util.keywords(objective):
                stem = util.stem(token)
                if stem and stem not in surface_by_stem:
                    surface_by_stem[stem] = token
            ordered_terms = [
                term for term in _meaningful_terms(objective)
                if term not in covered
            ]
            terms = set(ordered_terms)
            task_terms[task["id"]] = terms
            for term in ordered_terms:
                term_tasks.setdefault(term, []).append(task["id"])
                term_first_seen.setdefault(term, len(term_first_seen))

        shared_terms = [
            term for term, task_ids in term_tasks.items()
            if len(task_ids) >= min_tasks
        ]
        shared_terms.sort(
            key=lambda term: (
                -len(term_tasks[term]), term_first_seen[term], term
            )
        )

        # Several shared terms can describe the same task set. Treat that as
        # one cluster and keep a stable representative seed.
        clusters: dict[tuple[str, ...], str] = {}
        for term in shared_terms:
            key = tuple(term_tasks[term])
            clusters.setdefault(key, term)
        ordered_clusters = sorted(
            clusters.items(),
            key=lambda item: (
                -len(item[0]), item[1], item[0],
            ),
        )

        for task_ids, seed in ordered_clusters:
            counts: Counter[str] = Counter()
            for task_id in task_ids:
                counts.update(task_terms.get(task_id, set()))
            # Noisy real clusters share most, not all, of the seed tasks. Keep
            # a term when it appears in at least two thirds of the cluster.
            association = max(2, (2 * len(task_ids) + 2) // 3)
            cluster_terms = [
                term for term, count in counts.items()
                if count >= association
            ]
            cluster_terms.sort(
                key=lambda term: (
                    -counts[term], term_first_seen.get(term, 10**9), term
                )
            )
            surface_terms = [
                surface_by_stem.get(term, term)
                for term in cluster_terms[:MAX_CLUSTER_TERMS]
            ]
            terms = surface_terms
            if not terms:
                continue
            # Propose the user's surface spelling, not the stemmer output.
            name = proposed_name(surface_terms)
            if not name:
                continue
            if self._overlaps_existing(terms, name):
                continue
            if self._domain_name_exists(name):
                continue

            suggestion = self._create(
                name=name, terms=terms, task_ids=list(task_ids), now=now_dt
            )
            return {
                "status": "completed",
                "created": [suggestion],
                "examined": examined,
                "clusters": len(ordered_clusters),
            }

        return {
            "status": "completed", "created": [], "examined": examined,
            "clusters": len(ordered_clusters),
        }

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _now(value: dt.datetime | str | None) -> dt.datetime:
        if value is None:
            return util.now_utc()
        if isinstance(value, dt.datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=dt.timezone.utc)
            return value.astimezone(dt.timezone.utc)
        return util.parse_iso(str(value))

    def _window_days(self) -> int:
        try:
            value = int(self.config.get("domains.suggestions.window_days", 30))
        except (TypeError, ValueError):
            value = 30
        return max(1, min(MAX_WINDOW_DAYS, value))

    def _min_tasks(self) -> int:
        try:
            value = int(self.config.get("domains.suggestions.min_tasks", 6))
        except (TypeError, ValueError):
            value = 6
        return max(1, value)

    def _active_domain_terms(self) -> set[str]:
        covered: set[str] = set()
        for domain in self.domains.list_active():
            text_parts = [
                str(domain.get("name") or ""),
                str(domain.get("description") or ""),
                " ".join(str(item) for item in domain.get("keywords") or []),
                " ".join(str(item) for item in domain.get("examples") or []),
            ]
            covered.update(util.stemmed_keywords(" ".join(text_parts)))
        return covered

    def _overlaps_existing(self, terms: list[str], name: str) -> bool:
        proposed_terms = {util.stem(item) for item in terms}
        proposed_key = _name_key(name)
        for row in self.db.query("SELECT terms FROM domain_suggestions"):
            existing_terms = _decoded_list(row["terms"])
            if proposed_terms & {util.stem(item) for item in existing_terms}:
                return True
            existing_name = proposed_name(existing_terms)
            if existing_name and _name_key(existing_name) == proposed_key:
                return True
        return False

    def _domain_name_exists(self, name: str) -> bool:
        if self.domains.by_name(name) is not None:
            return True
        wanted = _name_key(name)
        if not wanted:
            return False
        return any(
            _name_key(str(domain.get("name") or "")) == wanted
            for domain in self.domains.list_all()
        )

    def _create(
        self,
        *,
        name: str,
        terms: list[str],
        task_ids: list[str],
        now: dt.datetime,
    ) -> dict[str, Any]:
        suggestion_id = util.new_id("dsug")
        created_at = util.iso(now)
        self.db.execute(
            "INSERT INTO domain_suggestions(id, terms, task_ids, status,"
            " created_at) VALUES (?,?,?,?,?)",
            (
                suggestion_id, util.dumps(terms), util.dumps(task_ids),
                "open", created_at,
            ),
        )
        self.events.record(
            "domain.suggestion",
            data={
                "id": suggestion_id,
                "name": name,
                "terms": list(terms),
                "task_count": len(task_ids),
            },
        )
        objectives: list[str] = []
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            objectives = [
                str(row["objective"])
                for row in self.db.query(
                    f"SELECT objective FROM tasks WHERE id IN ({placeholders})"
                    " ORDER BY last_activity_at DESC, id ASC LIMIT 3",
                    tuple(task_ids),
                )
            ]
        task_text = ""
        if objectives:
            task_text = " Tasks: " + "; ".join(
                util.clip(item, 80) for item in objectives
            ) + "."
        self.messages.record(
            "note",
            f'Suggested domain "{name}" from {len(task_ids)} General tasks. '
            f"Terms: {', '.join(terms)}.{task_text} Create it only if this "
            "work deserves its own space.",
            source="system",
            visible=True,
            meta={
                "kind": "domain.suggestion",
                "suggestion_id": suggestion_id,
                "terms": list(terms),
                "task_ids": list(task_ids),
                "name": name,
            },
        )
        return self.get(suggestion_id)  # type: ignore[return-value]

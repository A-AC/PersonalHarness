"""Persistent state: messages, events, domains, and tasks.

These are thin, explicit managers over SQLite rows. They hold no policy of
their own; the runtime and scheduler decide, these persist and query.

The messages table is the single durable interaction history. Every input —
web, CLI, schedule, heartbeat, system — becomes a row here with a source.
The ``visible`` flag keeps maintenance activity out of the ordinary chat view
without hiding it from inspection.
"""

from __future__ import annotations

from typing import Any

from . import util
from .config import (
    ROUTING_HINT_MIN_FALLBACK,
    ROUTING_MARGIN_FALLBACK,
    ROUTING_STEMMING_FALLBACK,
    ROUTING_THRESHOLD_FALLBACK,
)
from .db import Database, rows_to_dicts


# ---------------------------------------------------------------------------
# Messages: the unified interaction history
# ---------------------------------------------------------------------------

class Messages:
    def __init__(self, db: Database):
        self.db = db

    def record(
        self,
        role: str,
        content: str,
        *,
        source: str = "system",
        domain_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        visible: bool = True,
        meta: dict[str, Any] | None = None,
        message_id: int | None = None,
    ) -> int:
        with self.db.transaction() as conn:
            if message_id is not None:
                conn.execute(
                    "INSERT INTO messages(id, ts, role, content, source, domain_id,"
                    " task_id, run_id, visible, meta) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        message_id, util.iso(), role, content, source, domain_id,
                        task_id, run_id, int(visible), util.dumps(meta or {}),
                    ),
                )
                return message_id
            cur = conn.execute(
                "INSERT INTO messages(ts, role, content, source, domain_id,"
                " task_id, run_id, visible, meta) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    util.iso(), role, content, source, domain_id, task_id,
                    run_id, int(visible), util.dumps(meta or {}),
                ),
            )
            return int(cur.lastrowid)

    def get(self, message_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM messages WHERE id=?", (message_id,))
        return rows_to_dicts([row])[0] if row else None

    def list_visible(self, limit: int = 200, before_id: int | None = None) -> list[dict[str, Any]]:
        if before_id:
            rows = self.db.query(
                "SELECT * FROM messages WHERE visible=1 AND id<? "
                "ORDER BY id DESC LIMIT ?",
                (before_id, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM messages WHERE visible=1 ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        out = rows_to_dicts(rows)
        out.reverse()
        return out

    def recent(
        self, limit: int = 20, include_hidden: bool = False,
        trusted_only: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages"
        clauses = []
        if not include_hidden:
            clauses.append("visible=1")
        if trusted_only:
            clauses.extend([
                "COALESCE(json_extract(meta, '$.tainted'), 0)=0",
                "COALESCE(json_extract(meta, '$.derived_untrusted'), 0)=0",
            ])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        out = rows_to_dicts(self.db.query(sql, (limit,)))
        out.reverse()
        return out

    def for_task(self, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self.db.query(
                "SELECT * FROM messages WHERE task_id=? ORDER BY id ASC LIMIT ?",
                (task_id, limit),
            )
        )

    def recent_for_task(
        self, task_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """The most recent messages for one task, oldest first."""
        out = rows_to_dicts(
            self.db.query(
                "SELECT * FROM messages WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, limit),
            )
        )
        out.reverse()
        return out

    def search(
        self, query: str, limit: int = 5, exclude_task: str | None = None
    ) -> list[dict[str, Any]]:
        """FTS5 search over the durable history (old, buried conversations)."""
        terms = [t for t in util.keywords(query)][:8]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = self.db.query(
                "SELECT m.* FROM messages_fts f JOIN messages m ON m.id=f.rowid "
                "WHERE messages_fts MATCH ? AND m.visible=1 "
                "ORDER BY rank LIMIT ?",
                (match, limit * 3),
            )
        except Exception:
            return []
        out = []
        for row in rows_to_dicts(rows):
            if exclude_task and row.get("task_id") == exclude_task:
                continue
            out.append(row)
            if len(out) >= limit:
                break
        return out


# ---------------------------------------------------------------------------
# Events: the observability log
# ---------------------------------------------------------------------------

class Events:
    def __init__(self, db: Database):
        self.db = db

    def record(
        self,
        type: str,
        *,
        level: str = "info",
        source: str | None = None,
        domain_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        schedule_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO event_log(ts, type, level, source, domain_id, task_id,"
            " run_id, schedule_id, data) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                util.iso(), type, level, source, domain_id, task_id, run_id,
                schedule_id, util.dumps(data or {}),
            ),
        )

    def list(self, limit: int = 100, level: str | None = None) -> list[dict[str, Any]]:
        if level:
            rows = self.db.query(
                "SELECT * FROM event_log WHERE level IN ('info',?) "
                "ORDER BY id DESC LIMIT ?",
                (level, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (limit,)
            )
        out = rows_to_dicts(rows)
        out.reverse()
        for row in out:
            row["data"] = util.loads(row.get("data") or "{}", {})
        return out

    def last_of_type(self, type: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM event_log WHERE type=? ORDER BY id DESC LIMIT 1", (type,)
        )
        return rows_to_dicts([row])[0] if row else None


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------

GENERAL_DOMAIN = "general"

DOMAIN_MAX_KEYWORDS = 24
DOMAIN_KEYWORD_MAX_CHARS = 48
DOMAIN_MAX_EXAMPLES = 24
DOMAIN_EXAMPLE_MAX_CHARS = 160
DEFAULT_INSTRUCTIONS_MAX_CHARS = 1200


def _to_public(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Decode the additive v9 JSON columns on a domain row.

    Existing callers see every field they saw before plus ``keywords`` and
    ``examples`` as lists and ``instructions`` as a string; a missing or
    malformed value degrades to an empty list/string rather than raising.
    """
    if row is None:
        return None
    out = dict(row)
    for field in ("keywords", "examples"):
        values = util.loads(out.get(field) or "[]", [])
        out[field] = [str(item) for item in values] if isinstance(values, list) else []
    out["instructions"] = out.get("instructions") or ""
    out["summary"] = out.get("summary") or ""
    out["summary_updated_at"] = out.get("summary_updated_at")
    return out


def _clean_terms(
    values: Any, *, field: str, limit: int, max_chars: int
) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ValueError(f"domain {field} must be a list of strings")
    items = list(values)
    if len(items) > limit:
        raise ValueError(f"domain supports at most {limit} {field}")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"domain {field} entries must be non-empty strings"
            )
        item = item.strip()
        if len(item) > max_chars:
            raise ValueError(
                f"domain {field} entries must be at most {max_chars} characters"
            )
        out.append(item)
    return out


def _clean_instructions(value: Any, config=None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("domain instructions must be a string")
    if "\x00" in value:
        raise ValueError("domain instructions must not contain NUL bytes")
    limit = DEFAULT_INSTRUCTIONS_MAX_CHARS
    if config is not None:
        try:
            limit = int(
                config.get("domains.instructions_max_chars", limit)
            )
        except (TypeError, ValueError):
            limit = DEFAULT_INSTRUCTIONS_MAX_CHARS
    return util.clip(value, limit)


def _clean_summary(value: Any, config=None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("domain summary must be a string")
    if "\x00" in value:
        raise ValueError("domain summary must not contain NUL bytes")
    limit = 800
    if config is not None:
        try:
            limit = int(config.get("routing.card_summary_chars", limit))
        except (TypeError, ValueError):
            limit = 800
    return util.clip(value, max(1, limit))


class Domains:
    """Domain records and deterministic routing.

    A domain is a durable area of work. Creation has a high threshold: only an
    explicit user command (via API/CLI) or an approved model proposal creates
    one. Routing never creates domains; unmatched input goes to ``general``.
    """

    def __init__(self, db: Database, events: Events, config=None):
        self.db = db
        self.events = events
        self.config = config

    def ensure_defaults(self) -> None:
        if not self.get(GENERAL_DOMAIN):
            self.create(
                GENERAL_DOMAIN,
                name="General",
                description="Default domain for work that fits nowhere else.",
                created_by="system",
            )

    def create(
        self,
        domain_id: str,
        *,
        name: str,
        description: str = "",
        created_by: str = "user",
        seed_note: str | None = None,
        keywords: list[str] | None = None,
        examples: list[str] | None = None,
        instructions: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        keyword_list = _clean_terms(
            keywords, field="keywords", limit=DOMAIN_MAX_KEYWORDS,
            max_chars=DOMAIN_KEYWORD_MAX_CHARS,
        )
        example_list = _clean_terms(
            examples, field="examples", limit=DOMAIN_MAX_EXAMPLES,
            max_chars=DOMAIN_EXAMPLE_MAX_CHARS,
        )
        instruction_text = _clean_instructions(instructions, self.config)
        summary_text = _clean_summary(summary, self.config)
        ts = util.iso()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO domains(id, name, description, status, created_at,"
                " updated_at, meta, keywords, examples, instructions, summary,"
                " summary_updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    domain_id, name, description, "active", ts, ts,
                    util.dumps({"created_by": created_by}),
                    util.dumps(keyword_list), util.dumps(example_list),
                    instruction_text, summary_text, ts if summary_text else None,
                ),
            )
            if seed_note:
                conn.execute(
                    "INSERT INTO memories(id, scope, domain_id, kind, content,"
                    " status, origin, provenance, created_at, updated_at, revision)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        util.new_id("mem"), f"domain:{domain_id}", domain_id,
                        "context", seed_note, "active", "user",
                        util.dumps({"source": "domain_seed"}),
                        ts, ts, 1,
                    ),
                )
        self.events.record(
            "domain.created", domain_id=domain_id,
            data={
                "name": name,
                "created_by": created_by,
                "keywords": len(keyword_list),
                "examples": len(example_list),
                "has_instructions": bool(instruction_text),
                "has_summary": bool(summary_text),
            },
        )
        return self.get(domain_id)  # type: ignore[return-value]

    def get(self, domain_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM domains WHERE id=?", (domain_id,))
        return _to_public(rows_to_dicts([row])[0]) if row else None

    def by_name(self, name: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM domains WHERE lower(name)=lower(?)", (name.strip(),)
        )
        return _to_public(rows_to_dicts([row])[0]) if row else None

    def list_active(self) -> list[dict[str, Any]]:
        rows = rows_to_dicts(
            self.db.query("SELECT * FROM domains WHERE status='active' ORDER BY name")
        )
        return [item for item in map(_to_public, rows) if item is not None]

    def list_all(self) -> list[dict[str, Any]]:
        rows = rows_to_dicts(self.db.query("SELECT * FROM domains ORDER BY name"))
        return [item for item in map(_to_public, rows) if item is not None]

    # -- routing ----------------------------------------------------------

    def route(
        self,
        text: str,
        *,
        open_tasks: list[dict[str, Any]] | None = None,
        recent_messages: list[dict[str, Any]] | None = None,
        learned: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Pick the domain for a new message. Deterministic and recorded.

        Evidence v2 scores the domain name, user keywords, example phrases,
        description, and learned vocabulary. Learned vocabulary combines the
        caller's memory/skill affinity with open- and past-task evidence.
        Every matched term is retained in the per-candidate breakdown so a
        routing decision can be explained without a model call.

        Routing never creates a domain. A clear non-General winner above the
        threshold wins; weak or ambiguous evidence falls back to ``general``.
        """
        words = self._routing_terms(text)
        surface_forms = self._surface_forms(text)
        if not words:
            return {
                "domain_id": GENERAL_DOMAIN,
                "score": 0.0,
                "reason": "empty message",
                "candidates": [],
                "breakdown": self._empty_breakdown(),
                "signals": {},
                "matched": [],
                "hint": None,
            }

        learned_terms = self._merge_learned_terms(
            text,
            learned=learned,
            open_tasks=open_tasks,
            recent_messages=recent_messages,
        )
        weights = self._routing_weights()
        candidates: list[dict[str, Any]] = []
        for domain in self.list_active():
            candidate = self._score_domain(
                domain,
                words,
                learned_terms.get(domain["id"], set()),
                weights,
                surface_forms,
            )
            if candidate["score"] > 0:
                candidates.append(candidate)

        candidates.sort(
            key=lambda item: (-float(item["score"]), str(item["domain_id"]))
        )
        specific = [
            item for item in candidates if item["domain_id"] != GENERAL_DOMAIN
        ]
        best = specific[0] if specific else None
        runner_up = specific[1] if len(specific) > 1 else None
        threshold = float(self._threshold())
        margin = float(self._margin())

        chosen = GENERAL_DOMAIN
        reason = "no domain evidence above threshold"
        if best is not None and float(best["score"]) >= threshold:
            if runner_up is not None and (
                float(best["score"]) - float(runner_up["score"]) < margin
            ):
                reason = "ambiguous domain evidence; fell back to general"
            else:
                chosen = str(best["domain_id"])
                reason = "best domain evidence"

        winner = next(
            (
                item for item in candidates
                if item["domain_id"] == chosen
            ),
            None,
        )
        winner_breakdown = (
            winner["breakdown"] if winner is not None
            else self._empty_breakdown()
        )
        matched = list(winner_breakdown.get("matched") or [])

        hint = None
        if chosen == GENERAL_DOMAIN and best is not None:
            hint_min = float(self._hint_min_score())
            if float(best["score"]) >= hint_min:
                hint = {
                    "domain_id": best["domain_id"],
                    "name": best["name"],
                    "score": best["score"],
                    "matched": list(
                        best["breakdown"].get("matched") or []
                    ),
                }

        return {
            "domain_id": chosen,
            "score": float(winner["score"]) if winner is not None else 0.0,
            "reason": reason,
            "candidates": candidates[:3],
            "breakdown": winner_breakdown,
            "signals": winner_breakdown.get("signals") or {},
            "matched": matched,
            "hint": hint,
        }

    def task_affinity(
        self, text: str, limit: int = 200
    ) -> dict[str, list[str]]:
        """Return learned task terms grouped by domain.

        Recent non-subagent task objectives are inspected in deterministic
        activity order. Only terms shared with the message are returned, at
        most three per domain; General is excluded because it is global.
        """
        words = self._routing_terms(text)
        if not words or limit <= 0:
            return {}
        rows = self.db.query(
            "SELECT id, domain_id, objective FROM tasks "
            "WHERE COALESCE(role, 'user') != 'subagent' "
            "AND domain_id IS NOT NULL AND domain_id != ? "
            "ORDER BY last_activity_at DESC, id ASC LIMIT ?",
            (GENERAL_DOMAIN, max(1, int(limit))),
        )
        affinity: dict[str, list[str]] = {}
        for row in rows_to_dicts(rows):
            domain_id = str(row.get("domain_id") or "")
            if not domain_id or domain_id == GENERAL_DOMAIN:
                continue
            overlap = words & self._routing_terms(str(row.get("objective") or ""))
            if not overlap:
                continue
            bucket = affinity.setdefault(domain_id, [])
            for term in sorted(overlap):
                if term not in bucket and len(bucket) < 3:
                    bucket.append(term)
        return {domain_id: affinity[domain_id] for domain_id in sorted(affinity)}

    def _merge_learned_terms(
        self,
        text: str,
        *,
        learned: dict[str, list[str]] | None,
        open_tasks: list[dict[str, Any]] | None,
        recent_messages: list[dict[str, Any]] | None,
    ) -> dict[str, set[str]]:
        """Merge every learned-vocabulary source, de-duplicated per domain."""
        merged: dict[str, set[str]] = {}

        def add(domain_id: Any, terms: Any) -> None:
            domain = str(domain_id or "")
            if not domain or domain == GENERAL_DOMAIN:
                return
            bucket = merged.setdefault(domain, set())
            bucket.update(self._routing_terms(" ".join(
                str(term) for term in (terms or [])
            )))

        for domain_id, terms in (learned or {}).items():
            add(domain_id, terms)

        # Past task objectives are learned evidence even when the caller did
        # not supply affinity explicitly.
        for domain_id, terms in self.task_affinity(text).items():
            add(domain_id, terms)

        message_words = self._routing_terms(text)
        for task in open_tasks or []:
            domain_id = task.get("domain_id")
            if not domain_id:
                continue
            overlap = message_words & self._routing_terms(
                str(task.get("objective") or "")
            )
            add(domain_id, overlap)

        # ``recent_messages`` remains a compatibility parameter, but its
        # overlaps are intentionally not learned evidence: status chips and
        # chat chatter must not make routing sticky. Past task objectives
        # are covered by ``task_affinity`` above.
        return merged

    def _score_domain(
        self,
        domain: dict[str, Any],
        words: set[str],
        learned: set[str],
        weights: dict[str, float],
        surface_forms: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Score one domain and retain the evidence behind every term."""
        name_terms = self._routing_terms(str(domain.get("name") or ""))
        keyword_terms = self._routing_terms(
            " ".join(str(item) for item in domain.get("keywords") or [])
        )
        example_terms = self._routing_terms(
            " ".join(str(item) for item in domain.get("examples") or [])
        )
        description_terms = self._routing_terms(
            str(domain.get("description") or "")
        )

        name_matches = sorted(words & name_terms)
        keyword_matches = sorted(words & keyword_terms)
        example_matches = sorted(words & example_terms)
        # Description evidence is intentionally bounded: two terms is the
        # maximum, so a long description cannot dominate explicit evidence.
        description_matches = sorted(words & description_terms)[:2]
        learned_matches = sorted(words & learned)

        raw_signals = {
            "name": (name_matches, weights["name"] * len(name_matches)),
            "keyword": (
                keyword_matches, weights["keyword"] * len(keyword_matches)
            ),
            "example": (
                example_matches, weights["example"] * len(example_matches)
            ),
            "description": (
                description_matches,
                weights["description"] * len(description_matches),
            ),
        }
        # Learned vocabulary is deliberately weaker than explicit evidence.
        # A learned-only route needs at least two matching terms; one shared
        # term must never be enough to select a domain by itself.
        explicit_match_count = (
            len(name_matches) + len(keyword_matches)
            + len(example_matches) + len(description_matches)
        )
        learned_contribution = min(
            weights["learned_cap"],
            weights["learned_term"] * len(learned_matches),
        )
        if explicit_match_count == 0 and len(learned_matches) < 2:
            learned_contribution = 0.0
        raw_signals["learned"] = (learned_matches, learned_contribution)

        factor = 0.5 if domain["id"] == GENERAL_DOMAIN else 1.0
        signals: dict[str, dict[str, Any]] = {}
        for signal, (terms, raw_contribution) in raw_signals.items():
            contribution = raw_contribution * factor
            signals[signal] = {
                "terms": list(terms),
                "contribution": round(contribution, 4),
            }
        surface_forms = surface_forms or {}
        matched: list[str] = []
        for signal in ("name", "keyword", "example", "description", "learned"):
            for term in signals[signal]["terms"]:
                surface = surface_forms.get(term, term)
                if surface not in matched:
                    matched.append(surface)
        score = round(sum(float(item["contribution"]) for item in signals.values()), 4)
        breakdown = {
            "domain_id": domain["id"],
            "name": domain["name"],
            "score": score,
            "factor": factor,
            "signals": signals,
            "matched": matched,
        }
        return {
            "domain_id": domain["id"],
            "name": domain["name"],
            "score": score,
            "breakdown": breakdown,
            "signals": signals,
            "matched": matched,
        }

    def _routing_terms(self, text: str) -> set[str]:
        terms = (
            util.stemmed_keywords(text)
            if self._stemming_enabled() else util.keywords(text)
        )
        return set(terms)

    def _stemming_enabled(self) -> bool:
        if self.config is None:
            return bool(ROUTING_STEMMING_FALLBACK)
        return bool(self.config.get(
            "domains.stemming", ROUTING_STEMMING_FALLBACK
        ))

    def _surface_forms(self, text: str) -> dict[str, str]:
        """Map normalized message terms back to their original surfaces."""
        forms: dict[str, str] = {}
        stemming = self._stemming_enabled()
        for surface in util.keywords(text):
            normalized = util.stem(surface) if stemming else surface
            forms.setdefault(normalized, surface)
        return forms

    def _routing_weights(self) -> dict[str, float]:
        defaults = {
            "name": 1.5,
            "keyword": 1.0,
            "example": 0.75,
            "description": 0.5,
            "learned_term": 0.5,
            "learned_cap": 2.0,
        }
        if self.config is None:
            return defaults
        configured = self.config.get("domains.weights", {}) or {}
        if not isinstance(configured, dict):
            return defaults
        out = dict(defaults)
        for key, fallback in defaults.items():
            try:
                out[key] = float(configured.get(key, fallback))
            except (TypeError, ValueError):
                out[key] = fallback
        return out

    @staticmethod
    def _empty_breakdown() -> dict[str, Any]:
        empty_signal = {
            "terms": [],
            "contribution": 0.0,
        }
        return {
            "domain_id": GENERAL_DOMAIN,
            "name": "General",
            "score": 0.0,
            "factor": 0.5,
            "signals": {
                signal: dict(empty_signal)
                for signal in (
                    "name", "keyword", "example", "description", "learned",
                )
            },
            "matched": [],
        }

    def _threshold(self) -> float:
        if self.config is None:
            return float(ROUTING_THRESHOLD_FALLBACK)
        return float(self.config.get(
            "domains.routing_threshold", ROUTING_THRESHOLD_FALLBACK
        ))

    def _margin(self) -> float:
        if self.config is None:
            return float(ROUTING_MARGIN_FALLBACK)
        return float(self.config.get(
            "domains.ambiguity_margin", ROUTING_MARGIN_FALLBACK
        ))

    def _hint_min_score(self) -> float:
        if self.config is None:
            return float(ROUTING_HINT_MIN_FALLBACK)
        return float(self.config.get(
            "domains.hint_min_score", ROUTING_HINT_MIN_FALLBACK
        ))

    def update(
        self,
        domain_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        keywords: list[str] | None = None,
        examples: list[str] | None = None,
        instructions: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Edit domain metadata. Never changes the id or routing rules.

        ``None`` means "leave unchanged"; an empty list/string clears the
        field. Routing evidence changes affect later deterministic routing
        decisions; they never alter the domain id or create another domain.
        """
        current = self.get(domain_id)
        if current is None:
            raise KeyError(f"no such domain: {domain_id}")
        fields: dict[str, Any] = {}
        if name is not None and name.strip() and name.strip() != current["name"]:
            fields["name"] = name.strip()
        if description is not None and description != current["description"]:
            fields["description"] = description
        if status is not None:
            if status not in ("active", "archived"):
                raise ValueError("domain status must be active or archived")
            if status != current["status"]:
                fields["status"] = status
        if keywords is not None:
            keyword_list = _clean_terms(
                keywords, field="keywords", limit=DOMAIN_MAX_KEYWORDS,
                max_chars=DOMAIN_KEYWORD_MAX_CHARS,
            )
            if keyword_list != current.get("keywords", []):
                fields["keywords"] = util.dumps(keyword_list)
        if examples is not None:
            example_list = _clean_terms(
                examples, field="examples", limit=DOMAIN_MAX_EXAMPLES,
                max_chars=DOMAIN_EXAMPLE_MAX_CHARS,
            )
            if example_list != current.get("examples", []):
                fields["examples"] = util.dumps(example_list)
        if instructions is not None:
            instruction_text = _clean_instructions(instructions, self.config)
            if instruction_text != current.get("instructions", ""):
                fields["instructions"] = instruction_text
        if summary is not None:
            summary_text = _clean_summary(summary, self.config)
            if summary_text != current.get("summary", ""):
                fields["summary"] = summary_text
                fields["summary_updated_at"] = util.iso()
        if not fields:
            return current
        fields["updated_at"] = util.iso()
        sets = ", ".join(f"{key}=?" for key in fields)
        self.db.execute(
            f"UPDATE domains SET {sets} WHERE id=?",
            (*fields.values(), domain_id),
        )
        self.events.record(
            "domain.updated", domain_id=domain_id,
            data={"fields": sorted(k for k in fields if k != "updated_at")},
        )
        return self.get(domain_id)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Routing feedback
# ---------------------------------------------------------------------------

ROUTING_FEEDBACK_SOURCES = frozenset({"ui", "tool", "cli"})
ROUTING_FEEDBACK_MAX_MESSAGE_CHARS = 4000


class RoutingFeedback:
    """Durable labels for routing corrections and their eval export.

    Feedback rows are deliberately small and immutable: they capture the
    message, the route that was chosen, the user's corrected domain, and the
    routing evidence available at correction time.  They do not alter routing
    vocabulary; keyword suggestions are computed separately and never written
    automatically.
    """

    def __init__(self, db: Database, events: Events):
        self.db = db
        self.events = events

    def record(
        self,
        task_id: str | None,
        message_text: str,
        routed_domain: str,
        correct_domain: str,
        evidence: dict[str, Any] | None = None,
        source: str = "ui",
    ) -> dict[str, Any]:
        """Record one labelled routing correction."""
        if not isinstance(message_text, str) or not message_text.strip():
            raise ValueError("routing feedback message must not be empty")
        routed = str(routed_domain or "").strip()
        correct = str(correct_domain or "").strip()
        if not routed:
            raise ValueError("routing feedback needs a routed domain")
        if not correct:
            raise ValueError("routing feedback needs a correct domain")
        source = str(source or "").strip().lower()
        if source not in ROUTING_FEEDBACK_SOURCES:
            raise ValueError(
                "routing feedback source must be ui, tool, or cli"
            )
        if evidence is None:
            evidence_data: dict[str, Any] = {}
        elif isinstance(evidence, dict):
            evidence_data = evidence
        else:
            raise ValueError("routing feedback evidence must be a JSON object")

        feedback_id = util.new_id("rfb")
        created_at = util.iso()
        bounded_message = util.clip(
            message_text.strip(), ROUTING_FEEDBACK_MAX_MESSAGE_CHARS
        )
        self.db.execute(
            "INSERT INTO routing_feedback(id, task_id, message_text,"
            " routed_domain, correct_domain, evidence, source, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                feedback_id, str(task_id) if task_id else None,
                bounded_message, routed, correct, util.dumps(evidence_data),
                source, created_at,
            ),
        )
        self.events.record(
            "routing.feedback.recorded",
            task_id=str(task_id) if task_id else None,
            data={
                "feedback_id": feedback_id,
                "routed_domain": routed,
                "correct_domain": correct,
                "source": source,
            },
        )
        return self.get(feedback_id)  # type: ignore[return-value]

    @staticmethod
    def _to_public(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        evidence = util.loads(out.get("evidence") or "{}", {})
        out["evidence"] = evidence if isinstance(evidence, dict) else {}
        return out

    def get(self, feedback_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM routing_feedback WHERE id=?", (feedback_id,)
        )
        return self._to_public(rows_to_dicts([row])[0]) if row else None

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        """List feedback newest first, with a stable insertion-order tie-break."""
        bounded = max(0, int(limit))
        if bounded == 0:
            return []
        rows = self.db.query(
            "SELECT * FROM routing_feedback ORDER BY created_at DESC, rowid DESC"
            " LIMIT ?",
            (bounded,),
        )
        return [
            item for item in map(self._to_public, rows_to_dicts(rows))
            if item is not None
        ]

    def export_scenarios(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Export feedback as loader-compatible routing scenarios.

        The newest feedback comes first.  A specific active corrected domain
        is reproduced as a one-domain fixture; General and inactive domains
        intentionally use no fixture so the scenario stays honest about the
        environment that was actually available.
        """
        if limit is None:
            raw_rows = self.db.query(
                "SELECT * FROM routing_feedback"
                " ORDER BY created_at DESC, rowid DESC"
            )
            feedback_rows = [
                item for item in map(self._to_public, rows_to_dicts(raw_rows))
                if item is not None
            ]
        else:
            feedback_rows = self.list(limit=limit)
        domain_rows = rows_to_dicts(self.db.query("SELECT * FROM domains"))
        domains = {
            str(row.get("id") or ""): _to_public(row)
            for row in domain_rows
            if row.get("id")
        }
        by_name = {
            str((item or {}).get("name") or "").lower(): item
            for item in domains.values()
            if item is not None
        }

        active_fixture_domains = [
            {
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "keywords": list(item.get("keywords") or []),
                "examples": list(item.get("examples") or []),
            }
            for item in sorted(
                (
                    domain for domain in domains.values()
                    if domain is not None
                    and domain.get("status") == "active"
                    and str(domain.get("id") or "") != GENERAL_DOMAIN
                    and str(domain.get("name") or "").strip()
                ),
                key=lambda entry: str(entry.get("name") or ""),
            )[:12]
        ]
        scenarios: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for feedback in feedback_rows:
            correct_id = str(feedback.get("correct_domain") or "")
            domain = domains.get(correct_id) or by_name.get(correct_id.lower())
            correct_name = (
                str(domain.get("name") or "")
                if domain is not None else correct_id
            )
            key = (
                str(feedback.get("message_text") or ""),
                str(feedback.get("routed_domain") or ""),
                str(feedback.get("correct_domain") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            fixture_domains = [dict(item) for item in active_fixture_domains]
            if domain is not None and domain.get("status") == "active":
                correct_fixture = {
                    "name": str(domain.get("name") or ""),
                    "description": str(domain.get("description") or ""),
                    "keywords": list(domain.get("keywords") or []),
                    "examples": list(domain.get("examples") or []),
                }
                if str(domain.get("id") or "") != GENERAL_DOMAIN and all(
                    item.get("name") != correct_fixture["name"]
                    for item in fixture_domains
                ):
                    fixture_domains.append(correct_fixture)
            scenarios.append({
                "id": f"k_feedback_{feedback['id']}",
                "name": f"Routing feedback {feedback['id']}",
                "category": "routing",
                "severity": "high",
                "modes": ["deterministic"],
                "tags": ["feedback"],
                "description": (
                    "Routing correction recorded from "
                    f"{feedback.get('source') or 'unknown'}."
                ),
                "settings": {"domains": {"model_routing": "off"}},
                "fixtures": {"domains": fixture_domains},
                "steps": [{
                    "kind": "turn",
                    "input": str(feedback.get("message_text") or ""),
                    "script": [{"content": "ok"}],
                    "expect": {"routing": {"domain": correct_name}},
                }],
            })
        return scenarios


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

ACTIVE_STATUSES = ("active", "waiting_user", "waiting_approval", "paused")


class Tasks:
    def __init__(self, db: Database, events: Events):
        self.db = db
        self.events = events

    def create(
        self,
        *,
        objective: str,
        domain_id: str | None = None,
        origin: str = "user",
        origin_ref: str | None = None,
        meta: dict[str, Any] | None = None,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        parent_run_id: str | None = None,
        role: str = "user",
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            task_id = self.create_in_transaction(
                conn, objective=objective, domain_id=domain_id,
                origin=origin, origin_ref=origin_ref, meta=meta,
                task_id=task_id, parent_task_id=parent_task_id,
                parent_run_id=parent_run_id, role=role,
            )
        self.events.record(
            "task.created", task_id=task_id, domain_id=domain_id,
            data={"objective": objective[:200], "origin": origin,
                  "role": role, "parent_task_id": parent_task_id},
        )
        return self.get(task_id)  # type: ignore[return-value]

    def create_in_transaction(
        self,
        conn,
        *,
        objective: str,
        domain_id: str | None = None,
        origin: str = "user",
        origin_ref: str | None = None,
        meta: dict[str, Any] | None = None,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        parent_run_id: str | None = None,
        role: str = "user",
    ) -> str:
        """Insert a task inside an existing transaction; return its id."""
        task_id = task_id or util.new_id("tsk")
        ts = util.iso()
        if role not in ("user", "subagent"):
            raise ValueError("task role must be 'user' or 'subagent'")
        conn.execute(
            "INSERT INTO tasks(id, domain_id, objective, status, origin,"
            " origin_ref, created_at, updated_at, last_activity_at,"
            " parent_task_id, parent_run_id, role, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, domain_id, objective, "active", origin, origin_ref,
                ts, ts, ts, parent_task_id, parent_run_id, role,
                util.dumps(meta or {"notes": ""}),
            ),
        )
        return task_id

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return rows_to_dicts([row])[0] if row else None

    def set_status(
        self,
        task_id: str,
        status: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        ts = util.iso()
        self.db.execute(
            "UPDATE tasks SET status=?, status_reason=?, updated_at=?,"
            " last_activity_at=? WHERE id=?",
            (status, reason, ts, ts, task_id),
        )
        self.events.record(
            "task.status", task_id=task_id,
            data={"status": status, "reason": reason},
        )
        return self.get(task_id)  # type: ignore[return-value]

    def touch(self, task_id: str) -> None:
        self.db.execute(
            "UPDATE tasks SET last_activity_at=?, updated_at=? WHERE id=?",
            (util.iso(), util.iso(), task_id),
        )

    def set_meta(self, task_id: str, meta: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE tasks SET meta=?, updated_at=? WHERE id=?",
            (util.dumps(meta), util.iso(), task_id),
        )

    def meta(self, task_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT meta FROM tasks WHERE id=?", (task_id,))
        if not row:
            return {}
        return util.loads(row["meta"] or "{}", {})

    def increment_retry(self, task_id: str) -> int:
        self.db.execute(
            "UPDATE tasks SET retry_count=retry_count+1, updated_at=? WHERE id=?",
            (util.iso(), task_id),
        )
        row = self.db.query_one(
            "SELECT retry_count FROM tasks WHERE id=?", (task_id,)
        )
        return int(row["retry_count"]) if row else 0

    def set_retry(
        self, task_id: str, next_retry_at: str | None, *, reset: bool = False
    ) -> None:
        """Schedule (or clear) the next automatic retry for a task."""
        if reset:
            self.db.execute(
                "UPDATE tasks SET retry_count=0, next_retry_at=?, updated_at=?"
                " WHERE id=?",
                (next_retry_at, util.iso(), task_id),
            )
        else:
            self.db.execute(
                "UPDATE tasks SET next_retry_at=?, updated_at=? WHERE id=?",
                (next_retry_at, util.iso(), task_id),
            )

    def clear_retry(self, task_id: str, *, reset_count: bool = True) -> None:
        if reset_count:
            self.db.execute(
                "UPDATE tasks SET retry_count=0, next_retry_at=NULL,"
                " updated_at=? WHERE id=?",
                (util.iso(), task_id),
            )
        else:
            self.db.execute(
                "UPDATE tasks SET next_retry_at=NULL, updated_at=? WHERE id=?",
                (util.iso(), task_id),
            )

    def due_for_retry(self, now: str | None = None) -> list[dict[str, Any]]:
        """Paused tasks whose retry time has arrived, oldest due first."""
        now = now or util.iso()
        return rows_to_dicts(self.db.query(
            "SELECT * FROM tasks WHERE status='paused' AND next_retry_at"
            " IS NOT NULL AND next_retry_at<=? ORDER BY next_retry_at",
            (now,),
        ))

    def set_domain(self, task_id: str, domain_id: str) -> dict[str, Any]:
        self.db.execute(
            "UPDATE tasks SET domain_id=?, updated_at=? WHERE id=?",
            (domain_id, util.iso(), task_id),
        )
        self.events.record(
            "task.domain", task_id=task_id, domain_id=domain_id,
            data={"reason": "domain changed by user"},
        )
        return self.get(task_id)  # type: ignore[return-value]

    def list_by_status(self, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        return rows_to_dicts(
            self.db.query(
                f"SELECT * FROM tasks WHERE status IN ({marks}) "
                "ORDER BY last_activity_at DESC",
                statuses,
            )
        )

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self.db.query(
                "SELECT * FROM tasks ORDER BY last_activity_at DESC LIMIT ?",
                (limit,),
            )
        )

    def list_open(self, domain_id: str | None = None) -> list[dict[str, Any]]:
        if domain_id:
            return rows_to_dicts(
                self.db.query(
                    "SELECT * FROM tasks WHERE status IN (?,?,?,?) AND domain_id=?"
                    " ORDER BY last_activity_at DESC",
                    (*ACTIVE_STATUSES, domain_id),
                )
            )
        return self.list_by_status(ACTIVE_STATUSES)

    def children(self, parent_task_id: str) -> list[dict[str, Any]]:
        """Sub-agent tasks delegated by one parent task, oldest first."""
        return rows_to_dicts(
            self.db.query(
                "SELECT * FROM tasks WHERE parent_task_id=? ORDER BY created_at",
                (parent_task_id,),
            )
        )

    def find_by_origin_ref(self, origin_ref: str) -> list[dict[str, Any]]:
        """Tasks with an exact origin reference (sub-agent dedupe)."""
        return rows_to_dicts(
            self.db.query(
                "SELECT * FROM tasks WHERE origin_ref=? ORDER BY created_at",
                (origin_ref,),
            )
        )

    def find_resume_candidate(
        self,
        text: str,
        domain_id: str,
        *,
        margin: float = 0.5,
        recency_boost_s: float = 3600.0,
    ) -> dict[str, Any]:
        """Pick the unfinished task this message most plausibly continues.

        Prefers tasks waiting on the user (they expect an answer), then other
        open tasks in the routed domain, ranked by keyword overlap with the
        objective plus a recency bonus. Returns None when no candidate clears
        the bar, or a dict describing an ambiguous tie.
        """
        words = set(util.keywords(text))
        if not words:
            return {"task": None}
        # Consider every open task, but prefer the routed domain: a follow-up
        # that lands in ``general`` should still be able to resume work that
        # lives in a specific domain when the wording matches.
        open_tasks = self.list_open()
        waiting = [t for t in open_tasks if t["status"] == "waiting_user"]
        pool = waiting + [t for t in open_tasks if t["status"] != "waiting_user"]
        now = util.now_utc()
        scored: list[tuple[float, dict[str, Any]]] = []
        for task in pool[:20]:
            # Sub-agent work belongs to its parent; a user message must never
            # resume a child task directly.
            if task.get("role") == "subagent":
                continue
            overlap = len(words & set(util.keywords(task["objective"])))
            score = float(overlap)
            same_domain = bool(domain_id) and task.get("domain_id") == domain_id
            if same_domain:
                score += 0.75
            if task["status"] == "waiting_user" and (overlap or same_domain):
                # A task that asked a question expects the next message.
                score += 1.0
            age = (now - util.parse_iso(task["last_activity_at"])).total_seconds()
            if age < recency_boost_s:
                score += 0.5 * (1.0 - age / recency_boost_s)
            if score >= 1.0:
                scored.append((score, task))
        if not scored:
            return {"task": None}
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if len(scored) >= 2 and scored[0][0] - scored[1][0] < margin:
            top_domain_ambiguous = scored[0][0] >= 2.0
            if top_domain_ambiguous:
                return {
                    "task": None,
                    "ambiguous": [t["id"] for _, t in scored[:3]],
                }
        return {"task": scored[0][1], "score": scored[0][0]}

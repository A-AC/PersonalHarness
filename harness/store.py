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

    def recent(self, limit: int = 20, include_hidden: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages"
        if not include_hidden:
            sql += " WHERE visible=1"
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
    ) -> dict[str, Any]:
        ts = util.iso()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO domains(id, name, description, status, created_at,"
                " updated_at, meta) VALUES (?,?,?,?,?,?,?)",
                (
                    domain_id, name, description, "active", ts, ts,
                    util.dumps({"created_by": created_by}),
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
            data={"name": name, "created_by": created_by},
        )
        return self.get(domain_id)  # type: ignore[return-value]

    def get(self, domain_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM domains WHERE id=?", (domain_id,))
        return rows_to_dicts([row])[0] if row else None

    def by_name(self, name: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM domains WHERE lower(name)=lower(?)", (name.strip(),)
        )
        return rows_to_dicts([row])[0] if row else None

    def list_active(self) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self.db.query("SELECT * FROM domains WHERE status='active' ORDER BY name")
        )

    def list_all(self) -> list[dict[str, Any]]:
        return rows_to_dicts(self.db.query("SELECT * FROM domains ORDER BY name"))

    # -- routing ----------------------------------------------------------

    def route(
        self,
        text: str,
        *,
        open_tasks: list[dict[str, Any]] | None = None,
        recent_messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Pick the domain for a new message. Deterministic and recorded.

        Scores each active domain by evidence: keyword overlap of the message
        with the domain name/description, open task objectives in the domain,
        and recent message mentions. Routing to the right existing domain is
        preferred; anything below the threshold lands in ``general`` — a new
        topic never creates a domain by itself.
        """
        words = set(util.keywords(text))
        if not words:
            return {"domain_id": GENERAL_DOMAIN, "score": 0.0,
                    "candidates": [], "reason": "empty message"}

        scored: list[dict[str, Any]] = []
        for domain in self.list_active():
            score = 0.0
            name_words = set(util.keywords(domain["name"]))
            desc_words = set(util.keywords(domain["description"]))
            score += 1.5 * len(words & name_words)
            score += 0.5 * len(words & desc_words)
            task_hits = 0
            for task in open_tasks or []:
                if task.get("domain_id") != domain["id"]:
                    continue
                overlap = len(words & set(util.keywords(task["objective"])))
                if overlap >= 1:
                    task_hits += 1
            score += 0.75 * task_hits
            recent_hits = 0
            for message in recent_messages or []:
                if message.get("domain_id") == domain["id"]:
                    overlap = len(
                        words & set(util.keywords(message["content"]))
                    )
                    if overlap >= 1:
                        recent_hits += 1
            score += 0.25 * min(recent_hits, 4)
            if domain["id"] == GENERAL_DOMAIN:
                score *= 0.5  # general is the fallback, not a magnet
            if score > 0:
                scored.append({
                    "domain_id": domain["id"],
                    "name": domain["name"],
                    "score": round(score, 2),
                })
        scored.sort(key=lambda item: item["score"], reverse=True)
        threshold = float(self._threshold())
        margin = float(self._margin())
        if not scored or scored[0]["score"] < threshold:
            chosen, reason = GENERAL_DOMAIN, "no domain evidence above threshold"
        elif len(scored) > 1 and scored[0]["score"] - scored[1]["score"] < margin:
            chosen = GENERAL_DOMAIN
            reason = "ambiguous domain evidence; fell back to general"
        else:
            chosen = scored[0]["domain_id"]
            reason = "best domain evidence"
        return {
            "domain_id": chosen,
            "score": scored[0]["score"] if scored else 0.0,
            "candidates": scored[:3],
            "reason": reason,
        }

    def _threshold(self) -> float:
        if self.config is None:
            return 2.0
        return float(self.config.get("domains.routing_threshold", 2.0))

    def _margin(self) -> float:
        if self.config is None:
            return 0.75
        return float(self.config.get("domains.ambiguity_margin", 0.75))

    def update(
        self,
        domain_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Edit domain metadata. Never changes the id or routing rules."""
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
    ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            task_id = self.create_in_transaction(
                conn, objective=objective, domain_id=domain_id,
                origin=origin, origin_ref=origin_ref, meta=meta,
                task_id=task_id,
            )
        self.events.record(
            "task.created", task_id=task_id, domain_id=domain_id,
            data={"objective": objective[:200], "origin": origin},
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
    ) -> str:
        """Insert a task inside an existing transaction; return its id."""
        task_id = task_id or util.new_id("tsk")
        ts = util.iso()
        conn.execute(
            "INSERT INTO tasks(id, domain_id, objective, status, origin,"
            " origin_ref, created_at, updated_at, last_activity_at, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, domain_id, objective, "active", origin, origin_ref,
                ts, ts, ts, util.dumps(meta or {"notes": ""}),
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

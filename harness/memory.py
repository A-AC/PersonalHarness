"""Memory: capture, retrieval, controlled updates, consolidation support.

Storage model: SQLite rows are authoritative (identity, scope, kind, status,
provenance, timestamps, revisions). Long-form notes may live in markdown
artifacts under ``artifacts/``; the row owns the path and the revision
relationship, so there is exactly one writable source of truth per memory
(the row; the artifact is its rendered body).

Scopes: ``global`` and ``domain:<id>``. Kinds: fact, preference, decision,
procedure, constraint, open_question, context, temp. Explicit user
instructions to remember something are captured immediately with high-priority
provenance; nothing is saved automatically per message.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from . import util
from .db import Database, rows_to_dicts
from .store import Events

KINDS = (
    "fact", "preference", "decision", "procedure", "constraint",
    "open_question", "context", "temp",
)

# Classify an explicit "remember this" by wording when the model did not pass
# a kind. Deliberately small; wrong guesses are correctable and versioned.
_KIND_HINTS = (
    ("preference", ("prefer", "likes", "always want", "never want", "instead of")),
    ("procedure", ("steps", "how to", "procedure", "workflow", "recipe")),
    ("decision", ("decided", "we chose", "going with", "agreed to")),
    ("constraint", ("must", "never", "only", "required to", "has to")),
    ("open_question", ("need to find out", "unknown", "open question", "not sure")),
    ("fact", ("is", "are", "was", "has", "located", "runs on")),
)


class MemoryError(ValueError):
    pass


class MemoryManager:
    def __init__(
        self,
        db: Database,
        events: Events,
        artifacts_dir: Path,
        config,
    ):
        self.db = db
        self.events = events
        self.artifacts_dir = artifacts_dir
        self.config = config

    # -- helpers ----------------------------------------------------------

    def _row(self, memory_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM memories WHERE id=?", (memory_id,))
        return rows_to_dicts([row])[0] if row else None

    def get(self, memory_id: str) -> dict[str, Any] | None:
        """Fetch one memory regardless of status, including revisions later."""
        row = self._row(memory_id)
        return self._to_public(row) if row else None

    def _to_public(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        out["provenance"] = util.loads(row.get("provenance") or "{}", {})
        out["tags"] = util.loads(row.get("tags") or "[]", [])
        out["meta"] = util.loads(row.get("meta") or "{}", {})
        # Lifetime fields are normalized for the API/UI (SQLite stores ints).
        out["access_count"] = int(row.get("access_count") or 0)
        out["last_accessed_at"] = row.get("last_accessed_at")
        out["archived_at"] = row.get("archived_at")
        out["archived"] = bool(row.get("archived_at"))
        out["pinned"] = bool(row.get("pinned"))
        out["importance"] = int(row.get("importance") or 0)
        return out

    def _default_kind(self, content: str, kind: str | None) -> str:
        if kind in KINDS:
            return kind
        lowered = (content or "").lower()
        for candidate, markers in _KIND_HINTS:
            if any(marker in lowered for marker in markers):
                return candidate
        return "fact"

    def _scope_for(
        self,
        scope: str | None,
        domain_id: str | None,
        domains,
    ) -> tuple[str, str | None]:
        """Validate scope. The model's scope is a hint; invalid falls back."""
        if scope == "global":
            return "global", None
        if scope and scope.startswith("domain:"):
            candidate = scope.split(":", 1)[1]
            if domains.get(candidate):
                return scope, candidate
            raise MemoryError(f"unknown domain in scope: {candidate}")
        if domain_id and domains.get(domain_id):
            return f"domain:{domain_id}", domain_id
        return "global", None

    def _record_revision(
        self,
        row: dict[str, Any],
        after: dict[str, Any],
        changed_by: str,
        reason: str | None,
    ) -> None:
        self.db.execute(
            "INSERT INTO memory_revisions(memory_id, revision, changed_at,"
            " changed_by, reason, before, after) VALUES (?,?,?,?,?,?,?)",
            (
                row["id"], after["revision"], util.iso(), changed_by,
                reason, util.dumps(self._to_public(row)),
                util.dumps(self._to_public(after)),
            ),
        )

    # -- access accounting + lifetime -------------------------------------

    def record_access(
        self, ids: list[str] | tuple[str, ...], run_id: str | None = None
    ) -> dict[str, Any]:
        """Bump usage counters for memories that were actually disclosed.

        One bump per memory per run: repeated context builds, agent searches,
        or UI searches inside the same run never inflate the count. The
        ``memory_access`` row makes that durable across restarts. Raw FTS
        candidate scans must never call this; only included results do.
        """
        memory_ids = [str(item) for item in (ids or []) if str(item).strip()]
        if not memory_ids:
            return {"updated": 0, "memory_ids": []}
        # Preserve first-seen order while removing duplicates in one call.
        ordered = list(dict.fromkeys(memory_ids))
        # An ad-hoc access (API search, no run) still counts, but must not
        # collide with a real run id in the dedupe index.
        access_key = str(run_id) if run_id else f"adhoc:{util.new_id('acc')}"
        ts = util.iso()
        updated: list[str] = []
        with self.db.transaction() as conn:
            for memory_id in ordered:
                row = conn.execute(
                    "SELECT id FROM memories WHERE id=?", (memory_id,)
                ).fetchone()
                if row is None:
                    continue
                already = conn.execute(
                    "SELECT 1 FROM memory_access WHERE memory_id=? AND"
                    " run_id=?",
                    (memory_id, access_key),
                ).fetchone()
                if already is not None:
                    continue
                conn.execute(
                    "INSERT INTO memory_access(memory_id, run_id, accessed_at)"
                    " VALUES (?,?,?)",
                    (memory_id, access_key, ts),
                )
                conn.execute(
                    "UPDATE memories SET access_count=access_count+1,"
                    " last_accessed_at=? WHERE id=?",
                    (ts, memory_id),
                )
                updated.append(memory_id)
        if updated:
            self.events.record(
                "memory.accessed",
                data={
                    "memory_ids": updated[:20],
                    "count": len(updated),
                    "run_id": run_id,
                },
            )
        return {"updated": len(updated), "memory_ids": updated}

    def lifetime_days(self, access_count: int) -> float:
        """Deterministic retention window for a usage count."""
        base = float(self.config.get("memory.lifetime.base_days", 30))
        growth = float(self.config.get("memory.lifetime.growth", 0.5))
        minimum = float(self.config.get("memory.lifetime.min_days", 14))
        maximum = float(self.config.get("memory.lifetime.max_days", 1095))
        days = base * (1 + max(0, int(access_count))) ** max(0.0, growth)
        return max(minimum, min(maximum, days))

    def sweep_lifetime(self, now: dt.datetime | None = None) -> dict[str, Any]:
        """Archive memories whose retention window has elapsed.

        Deterministic and model-free: the anchor is ``last_accessed_at``
        (falling back to ``created_at``) and the window grows with the access
        count. Pinned rows and protected kinds are never archived. Bounded by
        ``memory.lifetime.sweep_batch`` per pass.
        """
        report: dict[str, Any] = {
            "status": "disabled", "examined": 0, "archived": [],
        }
        if not bool(self.config.get("memory.lifetime.enabled", True)):
            return report
        now = now or util.now_utc()
        batch = max(1, int(self.config.get("memory.lifetime.sweep_batch", 200)))
        protected = {
            str(kind) for kind in (
                self.config.get("memory.lifetime.protected_kinds", []) or []
            )
        }
        clauses = [
            "status='active'", "archived_at IS NULL",
            "COALESCE(pinned, 0)=0",
        ]
        args: list[Any] = []
        if protected:
            placeholders = ",".join("?" for _ in protected)
            clauses.append(f"kind NOT IN ({placeholders})")
            args.extend(sorted(protected))
        rows = rows_to_dicts(
            self.db.query(
                "SELECT * FROM memories WHERE " + " AND ".join(clauses)
                + " ORDER BY COALESCE(last_accessed_at, created_at) ASC"
                " LIMIT ?",
                (*args, batch),
            )
        )
        report["examined"] = len(rows)
        report["status"] = "completed"
        for row in rows:
            anchor = util.parse_iso(
                row.get("last_accessed_at") or row["created_at"]
            )
            age_days = (now - anchor).total_seconds() / 86400
            allowed = self.lifetime_days(int(row.get("access_count") or 0))
            if age_days <= allowed:
                continue
            archived = self.archive(
                row["id"], changed_by="harness",
                reason=(
                    f"lifetime expired: unused for {age_days:.1f} days, "
                    f"retention {allowed:.0f} days after "
                    f"{int(row.get('access_count') or 0)} access(es)"
                ),
            )
            report["archived"].append(archived["id"])
        self.events.record(
            "memory.swept",
            data={
                "examined": report["examined"],
                "archived": len(report["archived"]),
                "batch": batch,
            },
        )
        return report

    def archive(
        self, memory_id: str, *, changed_by: str = "user",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Archive an active memory. It stays retrievable on request."""
        row = self._row(memory_id)
        if row is None:
            raise MemoryError(f"no such memory: {memory_id}")
        if row.get("archived_at"):
            return self._to_public(row)
        if row["status"] != "active":
            raise MemoryError(
                f"memory {memory_id} is {row['status']}, not active"
            )
        after = dict(row)
        ts = util.iso()
        after["archived_at"] = ts
        after["revision"] = int(row["revision"]) + 1
        after["updated_at"] = ts
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE memories SET archived_at=?, revision=?, updated_at=?"
                " WHERE id=?",
                (after["archived_at"], after["revision"], ts, memory_id),
            )
        self._record_revision(
            row, after, changed_by, reason or "archived"
        )
        self.events.record(
            "memory.archived",
            data={"memory_id": memory_id, "changed_by": changed_by,
                  "reason": reason},
        )
        return self._to_public(self._row(memory_id))  # type: ignore[arg-type]

    def restore(
        self, memory_id: str, *, changed_by: str = "user",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Restore an archived memory (explicit retrieval also calls this)."""
        row = self._row(memory_id)
        if row is None:
            raise MemoryError(f"no such memory: {memory_id}")
        if not row.get("archived_at"):
            return self._to_public(row)
        after = dict(row)
        ts = util.iso()
        after["archived_at"] = None
        after["revision"] = int(row["revision"]) + 1
        after["updated_at"] = ts
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE memories SET archived_at=NULL, revision=?,"
                " updated_at=? WHERE id=?",
                (after["revision"], ts, memory_id),
            )
        self._record_revision(
            row, after, changed_by, reason or "restored"
        )
        self.events.record(
            "memory.restored",
            data={"memory_id": memory_id, "changed_by": changed_by,
                  "reason": reason},
        )
        return self._to_public(self._row(memory_id))  # type: ignore[arg-type]

    def set_pinned(
        self, memory_id: str, pinned: bool, *, changed_by: str = "user",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Pin (protect from lifetime sweeps) or unpin a memory."""
        row = self._row(memory_id)
        if row is None:
            raise MemoryError(f"no such memory: {memory_id}")
        wanted = 1 if pinned else 0
        if int(row.get("pinned") or 0) == wanted:
            return self._to_public(row)
        after = dict(row)
        ts = util.iso()
        after["pinned"] = wanted
        after["revision"] = int(row["revision"]) + 1
        after["updated_at"] = ts
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE memories SET pinned=?, revision=?, updated_at=?"
                " WHERE id=?",
                (wanted, after["revision"], ts, memory_id),
            )
        self._record_revision(
            row, after, changed_by,
            reason or ("pinned" if pinned else "unpinned"),
        )
        self.events.record(
            "memory.pinned" if pinned else "memory.unpinned",
            data={"memory_id": memory_id, "changed_by": changed_by},
        )
        return self._to_public(self._row(memory_id))  # type: ignore[arg-type]

    # -- capture ----------------------------------------------------------

    def remember(
        self,
        content: str,
        *,
        kind: str | None = None,
        scope: str | None = None,
        domain_id: str | None = None,
        origin: str = "user",
        provenance: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        expires_at: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        domains=None,
        artifact_body: str | None = None,
    ) -> dict[str, Any]:
        """Create a memory. Correction of an existing subject supersedes it."""
        content = (content or "").strip()
        if not content:
            raise MemoryError("memory content is empty")
        kind = self._default_kind(content, kind)
        if kind not in KINDS:
            raise MemoryError(f"unknown memory kind: {kind}")
        scope_value, scope_domain = self._scope_for(scope, domain_id, domains)
        if kind == "temp" and not expires_at:
            days = int(self.config.get("memory.temp_expiry_days", 14))
            expires_at = util.iso(util.now_utc() + dt.timedelta(days=days))

        # Duplicate: identical active content in the same scope.
        existing = self.db.query_one(
            "SELECT * FROM memories WHERE scope=? AND status='active'"
            " AND content=?",
            (scope_value, content),
        )
        if existing:
            row = rows_to_dicts([existing])[0]
            return {"memory": self._to_public(row), "duplicate": True}

        # Conflict: same subject (kind + strong keyword overlap) in scope —
        # the newer statement supersedes the older one.
        superseded = self._find_conflict(content, scope_value, kind)

        ts = util.iso()
        memory_id = util.new_id("mem")
        artifact_path = None
        if artifact_body and len(artifact_body) > 400:
            artifact_path = self._write_artifact(memory_id, artifact_body)

        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO memories(id, scope, domain_id, kind, content,"
                " status, origin, provenance, artifact_path, created_at,"
                " updated_at, expires_at, revision, supersedes_id, tags)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    memory_id, scope_value, scope_domain, kind, content,
                    "active", origin, util.dumps(provenance or {}),
                    artifact_path, ts, ts, expires_at, 1,
                    superseded["id"] if superseded else None,
                    util.dumps(tags or []),
                ),
            )
            if superseded:
                conn.execute(
                    "UPDATE memories SET status='superseded', superseded_by=?,"
                    " updated_at=? WHERE id=?",
                    (memory_id, ts, superseded["id"]),
                )
        row = self._row(memory_id)
        if superseded:
            superseded_after = dict(superseded)
            superseded_after["status"] = "superseded"
            superseded_after["superseded_by"] = memory_id
            superseded_after["revision"] = int(superseded["revision"]) + 1
            superseded_after["updated_at"] = ts
            self._record_revision(
                superseded, superseded_after, changed_by=origin,
                reason="superseded by a newer statement",
            )
        self.events.record(
            "memory.created", task_id=task_id, run_id=run_id,
            data={
                "memory_id": memory_id, "kind": kind, "scope": scope_value,
                "superseded": superseded["id"] if superseded else None,
                "origin": origin,
            },
        )
        return {"memory": self._to_public(row), "duplicate": False,
                "superseded": superseded}

    def _find_conflict(
        self, content: str, scope: str, kind: str
    ) -> dict[str, Any] | None:
        """Same scope+kind with >= 60% keyword overlap looks like a
        correction of the same subject; the newest statement wins."""
        words = set(util.keywords(content))
        if len(words) < 2:
            return None
        for row in rows_to_dicts(
            self.db.query(
                "SELECT * FROM memories WHERE scope=? AND kind=? AND"
                " status='active'",
                (scope, kind),
            )
        ):
            other = set(util.keywords(row["content"]))
            if not other:
                continue
            overlap = len(words & other) / max(1, len(words | other))
            if overlap >= 0.5:
                return row
        return None

    @staticmethod
    def _fuller_statement(first: str, second: str) -> str:
        """Default merge content: the more informative of two duplicates.

        Duplicates state the same thing, so keeping one of them is enough;
        the longer text is the better bet at preserving the details. Models
        that want a blended wording pass ``content`` to ``merge``.
        """
        first, second = first.strip(), second.strip()
        return second if len(second) > len(first) else first

    def _write_artifact(self, memory_id: str, body: str) -> str:
        rel = f"memories/{memory_id}.md"
        path = self.artifacts_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return rel

    # -- controlled updates -----------------------------------------------

    def update(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        kind: str | None = None,
        expires_at: str | None = None,
        tags: list[str] | None = None,
        changed_by: str = "user",
        reason: str | None = None,
    ) -> dict[str, Any]:
        row = self._row(memory_id)
        if row is None:
            raise MemoryError(f"no such memory: {memory_id}")
        if row["status"] != "active":
            raise MemoryError(f"memory {memory_id} is {row['status']}, not active")
        after = dict(row)
        if content is not None and content.strip():
            after["content"] = content.strip()
        if kind:
            if kind not in KINDS:
                raise MemoryError(f"unknown memory kind: {kind}")
            after["kind"] = kind
        if expires_at is not None:
            after["expires_at"] = expires_at or None
        if tags is not None:
            after["tags"] = util.dumps(tags)
        after["revision"] = int(row["revision"]) + 1
        after["updated_at"] = util.iso()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE memories SET content=?, kind=?, expires_at=?, tags=?,"
                " revision=?, updated_at=? WHERE id=?",
                (
                    after["content"], after["kind"], after["expires_at"],
                    after["tags"], after["revision"], after["updated_at"],
                    memory_id,
                ),
            )
        self._record_revision(row, after, changed_by, reason)
        self.events.record(
            "memory.updated", data={"memory_id": memory_id,
            "revision": after["revision"], "changed_by": changed_by},
        )
        return self._to_public(self._row(memory_id))  # type: ignore[arg-type]

    def retract(
        self, memory_id: str, *, changed_by: str = "user", reason: str | None = None
    ) -> dict[str, Any]:
        row = self._row(memory_id)
        if row is None:
            raise MemoryError(f"no such memory: {memory_id}")
        after = dict(row)
        after["status"] = "retracted"
        after["revision"] = int(row["revision"]) + 1
        after["updated_at"] = util.iso()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE memories SET status=?, revision=?, updated_at=? WHERE id=?",
                ("retracted", after["revision"], after["updated_at"], memory_id),
            )
        self._record_revision(row, after, changed_by, reason)
        self.events.record(
            "memory.retracted", data={"memory_id": memory_id, "reason": reason}
        )
        return self._to_public(self._row(memory_id))  # type: ignore[arg-type]

    def merge(
        self,
        source_id: str,
        target_id: str,
        *,
        content: str | None = None,
        changed_by: str = "consolidation",
        reason: str | None = "merged duplicate",
    ) -> dict[str, Any]:
        """Fold source into target: one statement, source retracted.

        ``content`` may replace the target's text; without it the fuller of
        the two statements is kept. Merging never stitches the two texts
        together, so repeated merges cannot accumulate an "Also:" chain of
        restated facts.
        """
        source = self._row(source_id)
        target = self._row(target_id)
        if source is None or target is None:
            raise MemoryError("merge needs two existing memories")
        if source_id == target_id:
            raise MemoryError("cannot merge a memory into itself")
        if source["status"] != "active" or target["status"] != "active":
            raise MemoryError("merge needs two active memories")
        if source["scope"] != target["scope"] or source["kind"] != target["kind"]:
            raise MemoryError("merge requires the same scope and kind")
        merged = (content or "").strip() or self._fuller_statement(
            target["content"], source["content"]
        )
        return self.update(
            target_id, content=merged, changed_by=changed_by, reason=reason
        ), self.retract(
            source_id, changed_by=changed_by,
            reason=f"merged into {target_id}",
        )

    # -- expiry -----------------------------------------------------------

    def expire_stale(self) -> int:
        now = util.iso()
        rows = rows_to_dicts(
            self.db.query(
                "SELECT id FROM memories WHERE status='active' AND expires_at"
                " IS NOT NULL AND expires_at < ?",
                (now,),
            )
        )
        for row in rows:
            self.retract(row["id"], changed_by="harness", reason="expired")
        return len(rows)

    # -- retrieval --------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        scope_priority: list[str] | None = None,
        limit: int | None = None,
        include_expired: bool = False,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """Explainable retrieval: FTS relevance + scope priority + recency.

        Superseded, retracted, and expired rows never enter results. Archived
        rows are excluded by default; with ``include_archived`` they return
        with a score penalty and an ``archived`` label, and the returned rows
        are restored (explicit retrieval) with an audit revision. Every result
        carries ``score_parts`` and a human-readable ``reason``.
        """
        limit = limit or int(self.config.get("memory.max_results", 8))
        terms = [t for t in util.keywords(query)][:10]
        now = util.now_utc()
        scope_priority = scope_priority or []
        results: list[dict[str, Any]] = []

        def usable(row: dict[str, Any]) -> bool:
            if row["status"] != "active":
                return False
            if row.get("archived_at") and not include_archived:
                return False
            return not self._expired(row, now)

        if terms:
            match = " OR ".join(f'"{t}"' for t in terms)
            try:
                rows = self.db.query(
                    "SELECT m.* FROM memories_fts f JOIN memories m"
                    " ON m.row_num=f.rowid WHERE memories_fts MATCH ?"
                    " ORDER BY rank LIMIT ?",
                    (match, limit * 4),
                )
            except Exception:
                rows = []
            for row in rows_to_dicts(rows):
                if not usable(row):
                    continue
                if not self._scope_visible(row, scope_priority):
                    continue
                parts = {
                    "fts": 1.0,
                    "scope": self._scope_bonus(row, scope_priority),
                    "recency": self._recency(row, now),
                }
                score = sum(parts.values())
                results.append(self._scored(row, score, parts, terms))
        # Always include a few recent active memories from the priority
        # scopes even without a lexical hit (domain context).
        if len(results) < limit:
            known = {r["id"] for r in results}
            for scope in scope_priority:
                if len(results) >= limit:
                    break
                for row in rows_to_dicts(
                    self.db.query(
                        "SELECT * FROM memories WHERE scope=? AND status="
                        "'active' ORDER BY updated_at DESC LIMIT ?",
                        (scope, 6),
                    )
                ):
                    if row["id"] in known or not usable(row):
                        continue
                    known.add(row["id"])
                    results.append(self._scored(row, 0.4, {"scope": 0.4},
                                                terms))
        results.sort(key=lambda r: r["score"], reverse=True)
        final = results[:limit]
        if include_archived:
            for item in final:
                if not item.get("archived") or not item.get("archived_at"):
                    continue
                self.restore(
                    item["id"], changed_by="retrieval",
                    reason="explicit retrieval with include_archived",
                )
                item["restored"] = True
                item["restored_from_archive_at"] = item["archived_at"]
                item["archived_at"] = None
        return final

    def _scored(
        self, row: dict[str, Any], score: float, parts: dict[str, float],
        terms: list[str],
    ) -> dict[str, Any]:
        out = self._to_public(row)
        archived = bool(row.get("archived_at"))
        if archived:
            # Archived rows are retrievable but never outrank live memory.
            parts = {**parts, "archived": -0.75}
            score += -0.75
        out["score"] = round(score, 3)
        out["score_parts"] = {k: round(v, 3) for k, v in parts.items()}
        reason = (
            f"lexical match on {', '.join(terms[:4]) or 'recency'}"
            f"; scope={row['scope']}"
        )
        if archived:
            reason += (
                "; archived: included because archived memories were "
                "requested, and restored by this explicit retrieval"
            )
        out["reason"] = reason
        out["archived"] = archived
        return out

    def _expired(self, row: dict[str, Any], now: dt.datetime) -> bool:
        if not row.get("expires_at"):
            return False
        return util.parse_iso(row["expires_at"]) < now

    def _scope_visible(
        self, row: dict[str, Any], scope_priority: list[str]
    ) -> bool:
        # A lexical hit never widens scope. Global memory plus the active
        # domain's priority scope are visible; sibling domain scopes are not.
        return row["scope"] == "global" or row["scope"] in scope_priority

    def _scope_bonus(self, row: dict[str, Any], priority: list[str]) -> float:
        if row["scope"] in priority:
            return 0.6
        if row["scope"] == "global":
            return 0.3
        return 0.0

    def _recency(self, row: dict[str, Any], now: dt.datetime) -> float:
        age_days = (now - util.parse_iso(row["updated_at"])).total_seconds() / 86400
        return max(0.0, 0.5 - 0.01 * age_days)

    # -- listing / provenance --------------------------------------------

    def list(
        self, scope: str | None = None, limit: int = 100,
        status: str | None = "active",
    ) -> list[dict[str, Any]]:
        """List memories by status.

        ``status='active'`` (default) means live rows only; archived rows are
        separate. ``status='archived'`` lists only archived rows.
        ``status='any'`` (or ``None``) means every status, archived included.
        """
        clauses: list[str] = []
        args: list[Any] = []
        if scope:
            clauses.append("scope=?")
            args.append(scope)
        if status == "archived":
            clauses.append("status='active'")
            clauses.append("archived_at IS NOT NULL")
        elif status in (None, "any"):
            pass
        else:
            clauses.append("status=?")
            args.append(status)
            if status == "active":
                clauses.append("archived_at IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.query(
            f"SELECT * FROM memories{where} ORDER BY updated_at DESC LIMIT ?",
            (*args, limit),
        )
        return [self._to_public(r) for r in rows_to_dicts(rows)]

    def revisions(self, memory_id: str) -> list[dict[str, Any]]:
        rows = rows_to_dicts(
            self.db.query(
                "SELECT * FROM memory_revisions WHERE memory_id=?"
                " ORDER BY revision DESC",
                (memory_id,),
            )
        )
        for row in rows:
            row["before"] = util.loads(row.get("before") or "{}", {})
            row["after"] = util.loads(row.get("after") or "{}", {})
        return rows

    def read_artifact(self, rel_path: str, limit: int = 8000) -> str:
        """Bounded read of a memory artifact (path must be inside the
        artifacts directory; no traversal)."""
        base = self.artifacts_dir.resolve()
        candidate = (base / rel_path).resolve()
        if not str(candidate).startswith(str(base)):
            raise MemoryError("artifact path escapes the artifacts directory")
        if not candidate.is_file():
            raise MemoryError(f"missing artifact: {rel_path}")
        return util.clip(candidate.read_text(encoding="utf-8", errors="replace"), limit)

    def check_artifacts(self) -> list[dict[str, Any]]:
        """Detect rows referencing missing artifacts (DB is authoritative)."""
        problems = []
        rows = rows_to_dicts(self.db.query(
            "SELECT id, artifact_path FROM memories WHERE artifact_path"
            " IS NOT NULL AND status='active'"
        ))
        for row in rows:
            path = self.artifacts_dir / row["artifact_path"]
            if not path.is_file():
                problems.append({
                    "memory_id": row["id"],
                    "artifact_path": row["artifact_path"],
                    "problem": "missing",
                })
        return problems

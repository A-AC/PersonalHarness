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
        *,
        origin_domain_id: str | None = None,
    ) -> tuple[str, str | None]:
        """Validate scope and keep General unscoped.

        General is the unscoped interface, not a memory silo. New writes from
        General are therefore stored as global rows with no domain id. Legacy
        ``domain:general`` rows are left untouched and are normalised at read
        time by :meth:`_effective_scope`.

        ``origin_domain_id`` marks a model-authored write. In that path the
        model may narrow to the current domain or widen to global; a request
        for another domain is coerced to the originating task's domain. Direct
        manager calls (fixtures, consolidation) keep the historical behaviour.
        """
        current = str(domain_id or "")
        if current == "general":
            current = ""
        if origin_domain_id is not None:
            origin = str(origin_domain_id or "")
            if origin == "general":
                origin = ""
            requested = str(scope or "").strip()
            if requested == "global":
                return "global", None
            if origin and domains is not None and domains.get(origin):
                # Narrow to the current domain; another/unknown domain is
                # coerced because the model may not widen across domains.
                return f"domain:{origin}", origin
            return "global", None
        # Historical/direct path.
        if not current and not scope:
            return "global", None
        if scope == "global":
            return "global", None
        if scope and scope.startswith("domain:"):
            candidate = scope.split(":", 1)[1]
            if candidate == "general":
                return "global", None
            if domains is not None and domains.get(candidate):
                return scope, candidate
            if current and domains is not None and domains.get(current):
                return f"domain:{current}", current
            return "global", None
        if current and domains is not None and domains.get(current):
            return f"domain:{current}", current
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
        origin_domain_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a memory. Correction of an existing subject supersedes it."""
        content = (content or "").strip()
        if not content:
            raise MemoryError("memory content is empty")
        kind = self._default_kind(content, kind)
        if kind not in KINDS:
            raise MemoryError(f"unknown memory kind: {kind}")
        requested_scope = str(scope or "").strip()
        default_global = (
            origin_domain_id is not None
            and not requested_scope
            and kind in ("preference", "constraint")
        )
        if default_global:
            # Policy: facts about the user (preferences/constraints) default
            # to global; domain-work facts stay with the task domain.
            scope = "global"
        scope_value, scope_domain = self._scope_for(
            scope, domain_id, domains, origin_domain_id=origin_domain_id
        )
        provenance = dict(provenance or {})
        origin_for_record = (
            str(origin_domain_id) if origin_domain_id is not None else
            (str(domain_id) if domain_id else "")
        )
        if origin_for_record:
            provenance.setdefault("task_domain_id", origin_for_record)
            if domains is not None and origin_for_record != "general":
                origin_domain = domains.get(origin_for_record)
                if origin_domain is not None:
                    provenance.setdefault(
                        "task_domain", str(origin_domain.get("name") or "")
                    )
        provenance.setdefault(
            "scope_requested", requested_scope or "auto"
        )
        provenance.setdefault("scope_effective", scope_value)
        if origin_domain_id is not None:
            if default_global:
                provenance["scope_choice"] = "default_global"
            elif requested_scope == "global" and scope_value == "global":
                provenance["scope_choice"] = "widen_global"
            elif scope_value.startswith("domain:"):
                provenance["scope_choice"] = "narrow_domain"
            else:
                provenance["scope_choice"] = "global"
        if kind == "temp" and not expires_at:
            days = int(self.config.get("memory.temp_expiry_days", 14))
            expires_at = util.iso(util.now_utc() + dt.timedelta(days=days))

        # Duplicate: identical active content in the same scope.
        existing = self.db.query_one(
            "SELECT * FROM memories WHERE scope=? AND status='active'"
            " AND archived_at IS NULL AND content=?",
            (scope_value, content),
        )
        if existing:
            row = rows_to_dicts([existing])[0]
            return {
                "memory": self._to_public(row),
                "duplicate": True,
                "superseded": None,
            }

        # Similar text is only an advisory possible conflict. Lexical overlap
        # cannot prove a correction, so never supersede another row implicitly.
        possible_conflict = self._find_conflict(content, scope_value, kind)
        superseded = None

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
        row = self._row(memory_id)
        self.events.record(
            "memory.created", task_id=task_id, run_id=run_id,
            data={
                "memory_id": memory_id, "kind": kind, "scope": scope_value,
                "superseded": None,
                "possible_conflict_id": (
                    possible_conflict["id"] if possible_conflict else None
                ),
                "origin": origin,
            },
        )
        return {
            "memory": self._to_public(row), "duplicate": False,
            "superseded": None, "possible_conflict": possible_conflict,
        }

    def _find_conflict(
        self, content: str, scope: str, kind: str
    ) -> dict[str, Any] | None:
        """Return one possible lexical conflict for review, never a verdict.

        Archived rows are not candidates. Callers may surface this hint, but
        only an explicit update/merge may change or supersede existing memory.
        """
        words = set(util.keywords(content))
        if len(words) < 2:
            return None
        for row in rows_to_dicts(
            self.db.query(
                "SELECT * FROM memories WHERE scope=? AND kind=? AND"
                " status='active' AND archived_at IS NULL"
                " ORDER BY updated_at DESC",
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
        tainted_origin: str | None = None,
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
        if tainted_origin:
            provenance = util.loads(row.get("provenance") or "{}", {})
            provenance.update({
                "tainted": True,
                "tainted_origin": util.clip(tainted_origin, 300),
            })
            after["provenance"] = util.dumps(provenance)
        after["revision"] = int(row["revision"]) + 1
        after["updated_at"] = util.iso()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE memories SET content=?, kind=?, expires_at=?, tags=?,"
                " provenance=?, revision=?, updated_at=? WHERE id=?",
                (
                    after["content"], after["kind"], after["expires_at"],
                    after["tags"], after["provenance"], after["revision"],
                    after["updated_at"], memory_id,
                ),
            )
        self._record_revision(row, after, changed_by, reason)
        self.events.record(
            "memory.updated", data={"memory_id": memory_id,
            "revision": after["revision"], "changed_by": changed_by},
        )
        return self._to_public(self._row(memory_id))  # type: ignore[arg-type]

    def retract(
        self, memory_id: str, *, changed_by: str = "user",
        reason: str | None = None, tainted_origin: str | None = None,
    ) -> dict[str, Any]:
        row = self._row(memory_id)
        if row is None:
            raise MemoryError(f"no such memory: {memory_id}")
        after = dict(row)
        after["status"] = "retracted"
        if tainted_origin:
            provenance = util.loads(row.get("provenance") or "{}", {})
            provenance.update({
                "tainted": True,
                "tainted_origin": util.clip(tainted_origin, 300),
            })
            after["provenance"] = util.dumps(provenance)
        after["revision"] = int(row["revision"]) + 1
        after["updated_at"] = util.iso()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE memories SET status=?, provenance=?, revision=?,"
                " updated_at=? WHERE id=?",
                (
                    "retracted", after["provenance"], after["revision"],
                    after["updated_at"], memory_id,
                ),
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

    def domain_affinity(
        self, text: str, limit_per_domain: int = 3
    ) -> dict[str, list[str]]:
        """Return message terms learned from active domain memories.

        FTS finds candidate memories; the returned terms use the same
        ``domains.stemming`` normalization as domain routing. The query is
        read-only: it records no access, emits no event, and writes nothing.
        """
        query_terms = util.keywords(text)
        if not query_terms or limit_per_domain <= 0:
            return {}
        stemming = bool(self.config.get("domains.stemming", True))
        normalize = util.stemmed_keywords if stemming else util.keywords
        normalized_query = normalize(text)
        match = " OR ".join(f'"{term}"' for term in query_terms)
        try:
            rows = self.db.query(
                "SELECT m.* FROM memories_fts f JOIN memories m"
                " ON m.row_num=f.rowid WHERE memories_fts MATCH ? "
                "AND m.status='active' AND m.archived_at IS NULL "
                "AND (m.expires_at IS NULL OR m.expires_at > ?) "
                "AND m.scope LIKE 'domain:%' AND m.scope != 'domain:general' "
                "ORDER BY m.row_num ASC",
                (match, util.iso()),
            )
        except Exception:
            return {}

        wanted = set(normalized_query)
        affinity: dict[str, list[str]] = {}
        for row in rows_to_dicts(rows):
            domain_id = str(row.get("domain_id") or "")
            if not domain_id:
                scope = str(row.get("scope") or "")
                domain_id = scope.split(":", 1)[1] if ":" in scope else ""
            if not domain_id or domain_id == "general":
                continue
            content_terms = set(normalize(str(row.get("content") or "")))
            matched = wanted & content_terms
            if not matched:
                continue
            bucket = affinity.setdefault(domain_id, [])
            for term in normalized_query:
                if term in matched and term not in bucket:
                    if len(bucket) >= max(1, int(limit_per_domain)):
                        break
                    bucket.append(term)
        return {domain_id: affinity[domain_id] for domain_id in sorted(affinity)}

    def core_profile(
        self, limit_chars: int | None = None
    ) -> list[dict[str, Any]]:
        """Return the always-on profile made from pinned global memories.

        Legacy ``domain:general`` rows count as global at read time. The
        returned content is clipped cumulatively to ``limit_chars`` (or the
        configured ``memory.core_profile_chars``). This method is read-only:
        it records no access and emits no event.
        """
        if limit_chars is None:
            limit_chars = int(self.config.get("memory.core_profile_chars", 1200))
        limit_chars = max(0, int(limit_chars))
        if limit_chars <= 0:
            return []
        rows = rows_to_dicts(self.db.query(
            "SELECT * FROM memories WHERE status='active'"
            " AND archived_at IS NULL AND COALESCE(pinned, 0)=1"
            " AND scope IN ('global', 'domain:general')"
            " ORDER BY created_at DESC, row_num DESC"
        ))
        now = util.now_utc()
        remaining = limit_chars
        profile: list[dict[str, Any]] = []
        for row in rows:
            if self._expired(row, now):
                continue
            content = str(row.get("content") or "")
            if not content:
                continue
            if len(content) > remaining:
                content = content[:remaining]
            if not content:
                break
            item = self._to_public(row)
            item["content"] = content
            item["effective_scope"] = self._effective_scope(row)
            profile.append(item)
            remaining -= len(content)
            if remaining <= 0:
                break
        return profile

    def search(
        self,
        query: str,
        *,
        scope_priority: list[str] | None = None,
        limit: int | None = None,
        include_expired: bool = False,
        include_archived: bool = False,
        restore_archived: bool = False,
        cross_domain: bool = False,
    ) -> list[dict[str, Any]]:
        """Explainable retrieval: FTS relevance + scope priority + recency.

        ``scope_priority`` lists the current domain first and global second.
        With ``cross_domain=False`` (the default), only those priority scopes
        and global memory are visible. With ``cross_domain=True``, lexical
        hits from every active domain are eligible and labelled as
        cross-domain results. Other-domain rows are capped by
        ``memory.cross_domain_max`` and never enter the no-hit filler path.

        Superseded, retracted, and expired rows never enter results. Archived
        rows are excluded by default; with ``include_archived`` they return
        with a score penalty and an ``archived`` label without changing state.
        ``restore_archived=True`` additionally restores returned archived rows
        with an audit revision. Every result carries score parts and a reason.
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
            cross_cap = max(
                0, int(self.config.get("memory.cross_domain_max", 3))
            ) if cross_domain else 0
            candidate_limit = max(limit * 4, (limit + cross_cap) * 4, 20)
            try:
                rows = self.db.query(
                    "SELECT m.* FROM memories_fts f JOIN memories m"
                    " ON m.row_num=f.rowid WHERE memories_fts MATCH ?"
                    " ORDER BY rank LIMIT ?",
                    (match, candidate_limit),
                )
            except Exception:
                rows = []
            for row in rows_to_dicts(rows):
                if not usable(row):
                    continue
                if not self._scope_visible(
                    row, scope_priority, cross_domain=cross_domain
                ):
                    continue
                is_cross = self._is_other_domain(row, scope_priority)
                parts = {
                    "fts": 1.0,
                    "scope": self._scope_bonus(row, scope_priority),
                    "recency": self._recency(row, now),
                }
                if is_cross:
                    # A small, explicit boost keeps an attributed sibling hit
                    # below global and the current domain.
                    parts["cross_domain"] = 0.1
                score = sum(parts.values())
                results.append(self._scored(
                    row, score, parts, terms, cross_domain=is_cross
                ))

        # Always include a few recent active memories from the priority
        # scopes even without a lexical hit (domain context). Other domains
        # never enter this filler path. Legacy domain:general rows are read
        # as global and therefore share the global filler group.
        if len(results) < limit:
            known = {r["id"] for r in results}
            seen_groups: set[tuple[str, ...]] = set()
            for scope in scope_priority:
                if len(results) >= limit:
                    break
                if scope in ("global", "domain:general"):
                    group = ("global", "domain:general")
                else:
                    group = (scope,)
                if group in seen_groups:
                    continue
                seen_groups.add(group)
                placeholders = ",".join("?" for _ in group)
                for row in rows_to_dicts(
                    self.db.query(
                        "SELECT * FROM memories WHERE scope IN "
                        f"({placeholders}) AND status='active'"
                        " ORDER BY updated_at DESC LIMIT ?",
                        (*group, 6),
                    )
                ):
                    if row["id"] in known or not usable(row):
                        continue
                    if not self._scope_visible(row, scope_priority):
                        continue
                    known.add(row["id"])
                    bonus = self._scope_bonus(row, scope_priority)
                    results.append(self._scored(
                        row, bonus, {"scope": bonus}, terms
                    ))

        # Scope tiers are absolute: current domain first, global second,
        # attributed sibling domains last. Score breaks ties inside a tier.
        # This keeps a very recent sibling from outranking a current-domain
        # hit merely because recency has a wider range than the old bonus.
        results.sort(key=lambda item: (
            1 if item.get("effective_scope") == "global"
            else (2 if item.get("cross_domain") else 0),
            -float(item.get("score") or 0.0),
        ))
        if cross_domain:
            cross_cap = max(
                0, int(self.config.get("memory.cross_domain_max", 3))
            )
            cross_items = [
                item for item in results if item.get("cross_domain")
            ]
            allowed_cross = {
                item["id"] for item in cross_items[:cross_cap]
            }
            results = [
                item for item in results
                if not item.get("cross_domain") or item["id"] in allowed_cross
            ]
        final = results[:limit]
        if include_archived and restore_archived:
            for item in final:
                if not item.get("archived") or not item.get("archived_at"):
                    continue
                self.restore(
                    item["id"], changed_by="retrieval",
                    reason="explicit retrieval with include_archived",
                )
                item["restored"] = True
                item["reason"] = str(item.get("reason") or "") + (
                    "; restored by explicit request"
                )
                item["restored_from_archive_at"] = item["archived_at"]
                item["archived_at"] = None
        return final

    def _scored(
        self, row: dict[str, Any], score: float, parts: dict[str, float],
        terms: list[str], *, cross_domain: bool = False,
    ) -> dict[str, Any]:
        out = self._to_public(row)
        archived = bool(row.get("archived_at"))
        if archived:
            # Archived rows are retrievable but never outrank live memory.
            parts = {**parts, "archived": -0.75}
            score += -0.75
        effective_scope = self._effective_scope(row)
        out["score"] = round(score, 3)
        out["score_parts"] = {k: round(v, 3) for k, v in parts.items()}
        out["effective_scope"] = effective_scope
        out["cross_domain"] = bool(cross_domain)
        if effective_scope.startswith("domain:"):
            out["origin_domain_id"] = effective_scope.split(":", 1)[1]
        reason = (
            f"lexical match on {', '.join(terms[:4]) or 'recency'}"
            f"; scope={effective_scope}"
        )
        if cross_domain:
            reason += "; cross-domain lexical match"
        elif str(row.get("scope") or "") != effective_scope:
            reason += "; legacy domain:general treated as global"
        if archived:
            reason += (
                "; archived: included because archived memories were requested"
            )
        out["reason"] = reason
        out["archived"] = archived
        return out

    def _expired(self, row: dict[str, Any], now: dt.datetime) -> bool:
        if not row.get("expires_at"):
            return False
        return util.parse_iso(row["expires_at"]) < now

    def _effective_scope(self, row: dict[str, Any]) -> str:
        """Return the scope used for reads.

        General has no memory silo. Legacy ``domain:general`` rows are
        therefore global for visibility, ranking, and core-profile purposes;
        the stored scope is not rewritten.
        """
        scope = str(row.get("scope") or "")
        if scope == "domain:general":
            return "global"
        return scope

    def _scope_visible(
        self,
        row: dict[str, Any],
        scope_priority: list[str],
        cross_domain: bool = False,
    ) -> bool:
        """Central visibility rule for memory retrieval.

        Global memory (including legacy General rows) and the active domain's
        priority scopes are always visible. A cross-domain request may also
        see lexical hits from other *active* domains, but never archived or
        inactive domain scopes.
        """
        effective = self._effective_scope(row)
        if effective == "global" or effective in scope_priority:
            return True
        if not cross_domain or not effective.startswith("domain:"):
            return False
        domain_id = effective.split(":", 1)[1]
        if not domain_id:
            return False
        domain = self.db.query_one(
            "SELECT status FROM domains WHERE id=?", (domain_id,)
        )
        return domain is not None and str(domain["status"]) == "active"

    def _is_other_domain(
        self, row: dict[str, Any], priority: list[str]
    ) -> bool:
        effective = self._effective_scope(row)
        return (
            effective.startswith("domain:")
            and effective not in priority
        )

    def _scope_bonus(self, row: dict[str, Any], priority: list[str]) -> float:
        effective = self._effective_scope(row)
        if effective == "global":
            return 0.3
        if effective in priority:
            return 0.6
        return 0.0

    def _recency(self, row: dict[str, Any], now: dt.datetime) -> float:
        age_days = (now - util.parse_iso(row["updated_at"])).total_seconds() / 86400
        return max(0.0, 0.5 - 0.01 * age_days)

    # -- listing / provenance --------------------------------------------

    def list(
        self, scope: str | None = None, limit: int = 100,
        status: str | None = "active", pinned: bool | None = None,
    ) -> list[dict[str, Any]]:
        """List memories by status and optional filters.

        ``status='active'`` (default) means live rows only; archived rows are
        separate. ``status='archived'`` lists only archived rows.
        ``status='any'`` (or ``None``) means every status, archived included.
        ``scope='core'`` is the effective global scope, so legacy
        ``domain:general`` rows are included for the core-profile editor.
        ``pinned`` filters pinned (true) or unpinned (false) rows.
        """
        clauses: list[str] = []
        args: list[Any] = []
        if scope == "core":
            clauses.append("scope IN ('global', 'domain:general')")
        elif scope:
            clauses.append("scope=?")
            args.append(scope)
        if pinned is not None:
            clauses.append("COALESCE(pinned, 0)=?")
            args.append(1 if pinned else 0)
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

    def list_trusted(self, limit: int = 100) -> list[dict[str, Any]]:
        """Live, unarchived rows without tainted provenance, newest first."""
        rows = self.db.query(
            "SELECT * FROM memories WHERE status='active'"
            " AND archived_at IS NULL"
            " AND COALESCE(json_extract(provenance, '$.tainted'), 0)=0"
            " ORDER BY updated_at DESC LIMIT ?",
            (max(0, int(limit)),),
        )
        return [self._to_public(row) for row in rows_to_dicts(rows)]

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
        artifacts directory; no traversal).

        Containment is checked with a path-relative test, not a string
        prefix: a sibling such as ``artifacts_evil/`` shares the prefix but is
        outside the directory.
        """
        base = self.artifacts_dir.resolve()
        candidate = (base / rel_path).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            raise MemoryError(
                "artifact path escapes the artifacts directory"
            ) from None
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

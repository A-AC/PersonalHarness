"""SQLite: the authoritative state store.

One database file (WAL mode) holds messages, events, domains, tasks, runs,
memories, schedules, occurrences, approvals, and tool executions. Readable
artifacts (markdown notes, change logs, backups) live beside it under
``artifacts/`` and are referenced explicitly from rows. The database is the
single source of truth for structured state; artifacts hold only long-form
text whose identity is tracked by the row that references them.

Schema versioning: ``schema_meta.schema_version``; ``migrate()`` creates v1
and can grow additive migrations later. Never rebuild in place without a
backup path documented in the README.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import util

SCHEMA_VERSION = 5

# Phase 3 memory lifetime: additive columns on ``memories`` plus a small
# access table. ``archived_at`` marks an archived row without touching the
# existing status CHECK constraint (SQLite cannot alter one in place). The
# access table makes "one bump per run" durable across restarts.
MEMORY_LIFETIME_COLUMNS = (
    ("access_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_accessed_at", "TEXT"),
    ("archived_at", "TEXT"),
    ("pinned", "INTEGER NOT NULL DEFAULT 0"),
    ("importance", "INTEGER NOT NULL DEFAULT 0"),
)

MEMORY_ACCESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_access (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  accessed_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_access_run
  ON memory_access(memory_id, run_id);
CREATE INDEX IF NOT EXISTS idx_memory_access_mem
  ON memory_access(memory_id);
"""

# Created after the additive v4 columns exist, so upgrading a pre-v4 database
# never trips over a missing ``archived_at`` while the base schema runs.
MEMORY_LIFETIME_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_memories_archive ON memories(archived_at);
"""

# Phase 4 sub-agents: additive parent/role columns. Depth and counters stay in
# ``tasks.meta``; the columns are only the durable tree relationship.
SUBAGENT_TASK_COLUMNS = (
    ("parent_task_id", "TEXT"),
    ("parent_run_id", "TEXT"),
    ("role", "TEXT NOT NULL DEFAULT 'user'"),
)

SUBAGENT_RUN_COLUMNS = (
    ("parent_run_id", "TEXT"),
)

# Created after the additive v5 columns exist, for upgraded and fresh databases.
SUBAGENT_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id);
"""

MODULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS modules (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('skill','mcp','web')),
  name TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK(status IN ('draft','active','disabled','error')),
  scope TEXT NOT NULL DEFAULT 'global'
    CHECK(scope IN ('global','domain','adaptive')),
  manifest TEXT NOT NULL,
  content_path TEXT,
  origin TEXT NOT NULL,
  provenance TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_loaded_at TEXT,
  last_error TEXT,
  usage_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS module_bindings (
  module_id TEXT NOT NULL,
  domain_id TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  risk_override TEXT,
  PRIMARY KEY (module_id, domain_id)
);

CREATE TABLE IF NOT EXISTS module_tools (
  module_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  schema TEXT NOT NULL,
  risk TEXT NOT NULL,
  side_effects INTEGER NOT NULL DEFAULT 1,
  read_only INTEGER NOT NULL DEFAULT 0,
  last_seen TEXT NOT NULL,
  PRIMARY KEY (module_id, tool_name)
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('user','assistant','note')),
  content TEXT NOT NULL,
  source TEXT,
  domain_id TEXT,
  task_id TEXT,
  run_id TEXT,
  visible INTEGER NOT NULL DEFAULT 1,
  meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
  USING fts5(content, content='messages', content_rowid='id',
             tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content)
  VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content)
  VALUES ('delete', old.id, old.content);
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS event_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  type TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'info',
  source TEXT,
  domain_id TEXT,
  task_id TEXT,
  run_id TEXT,
  schedule_id TEXT,
  data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON event_log(ts);

CREATE TABLE IF NOT EXISTS domains (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  meta TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  domain_id TEXT,
  objective TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('active','waiting_user','waiting_approval','paused','completed','failed','cancelled')),
  origin TEXT NOT NULL,
  origin_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_activity_at TEXT NOT NULL,
  status_reason TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  parent_task_id TEXT,
  parent_run_id TEXT,
  role TEXT NOT NULL DEFAULT 'user',
  meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_domain ON tasks(domain_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  trigger TEXT NOT NULL,
  trigger_ref TEXT,
  status TEXT NOT NULL CHECK(status IN
    ('running','completed','failed','interrupted','waiting_user',
     'waiting_approval','limit_reached')),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  iterations INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  decision TEXT,
  transcript TEXT NOT NULL DEFAULT '[]',
  context_sources TEXT,
  error TEXT,
  parent_run_id TEXT,
  meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS memories (
  row_num INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  scope TEXT NOT NULL,
  domain_id TEXT,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK(status IN ('active','superseded','retracted')),
  origin TEXT NOT NULL,
  provenance TEXT NOT NULL,
  artifact_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  supersedes_id TEXT,
  superseded_by TEXT,
  tags TEXT,
  meta TEXT,
  access_count INTEGER NOT NULL DEFAULT 0,
  last_accessed_at TEXT,
  archived_at TEXT,
  pinned INTEGER NOT NULL DEFAULT 0,
  importance INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, status);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
  USING fts5(content, content='memories', content_rowid='row_num',
             tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content) VALUES (new.row_num, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content)
  VALUES ('delete', old.row_num, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content)
  VALUES ('delete', old.row_num, old.content);
  INSERT INTO memories_fts(rowid, content) VALUES (new.row_num, new.content);
END;

CREATE TABLE IF NOT EXISTS memory_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  changed_at TEXT NOT NULL,
  changed_by TEXT NOT NULL,
  reason TEXT,
  before TEXT,
  after TEXT
);
CREATE INDEX IF NOT EXISTS idx_revisions_mem ON memory_revisions(memory_id);
""" + MEMORY_ACCESS_SCHEMA + """

CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  prompt TEXT NOT NULL,
  domain_id TEXT,
  kind TEXT NOT NULL CHECK(kind IN ('once','interval','daily','weekly')),
  at_ts TEXT,
  interval_s REAL,
  time_of_day TEXT,
  weekday TEXT,
  tz TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  next_due_at TEXT,
  meta TEXT
);

CREATE TABLE IF NOT EXISTS schedule_occurrences (
  id TEXT PRIMARY KEY,
  schedule_id TEXT NOT NULL,
  due_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','submitted','skipped','failed')),
  submitted_at TEXT,
  task_id TEXT,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_occ_sched ON schedule_occurrences(schedule_id);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  tool_call_id TEXT,
  arguments TEXT NOT NULL,
  risk TEXT NOT NULL,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN
    ('pending','approved','rejected','cancelled','executed','expired')),
  created_at TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT,
  expires_at TEXT,
  notified_at TEXT,
  meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

CREATE TABLE IF NOT EXISTS tool_executions (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  tool_call_id TEXT,
  arguments TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('awaiting_approval','in_flight','succeeded','failed','denied','unknown')),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  result TEXT,
  error TEXT,
  approval_id TEXT,
  duration_ms INTEGER,
  meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_toolx_run ON tool_executions(run_id);
CREATE INDEX IF NOT EXISTS idx_toolx_status ON tool_executions(status);
""" + MODULE_SCHEMA


class Database:
    """Thread-safe SQLite wrapper. One writer process per data directory."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            timeout=30.0,
            isolation_level=None,  # autocommit; explicit BEGIN where it matters
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self.migrate()

    # -- schema -----------------------------------------------------------

    def migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema v{row['value']} is newer than this "
                    f"build (v{SCHEMA_VERSION}); upgrade the application"
                )
            else:
                self._migrate_to(int(row["value"]))
            # Safe for both fresh and upgraded databases: the column exists by
            # the time this runs.
            self._conn.executescript(MEMORY_LIFETIME_INDEX_SCHEMA)
            self._conn.executescript(SUBAGENT_INDEX_SCHEMA)

    def _migrate_to(self, version: int) -> None:
        """Apply additive migrations from ``version`` to SCHEMA_VERSION."""
        if version < 2:
            columns = {
                row["name"] for row in self._conn.execute(
                    "PRAGMA table_info(tasks)"
                )
            }
            if "next_retry_at" not in columns:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN next_retry_at TEXT"
                )
        if version < 3:
            # Additive: capabilities become module rows. The base migration
            # above already created them; rerunning here keeps the migration
            # self-contained for databases that predate this script.
            self._conn.executescript(MODULE_SCHEMA)
        if version < 4:
            # Additive memory lifetime columns; never rebuild the table or
            # touch the status CHECK constraint.
            columns = {
                row["name"] for row in self._conn.execute(
                    "PRAGMA table_info(memories)"
                )
            }
            for name, ddl in MEMORY_LIFETIME_COLUMNS:
                if name not in columns:
                    self._conn.execute(
                        f"ALTER TABLE memories ADD COLUMN {name} {ddl}"
                    )
            self._conn.executescript(MEMORY_ACCESS_SCHEMA)
        if version < 5:
            # Additive sub-agent tree columns; never rebuild a table and never
            # alter an existing CHECK constraint.
            for table, columns in (
                ("tasks", SUBAGENT_TASK_COLUMNS),
                ("runs", SUBAGENT_RUN_COLUMNS),
            ):
                existing = {
                    row["name"] for row in self._conn.execute(
                        f"PRAGMA table_info({table})"
                    )
                }
                for name, ddl in columns:
                    if name not in existing:
                        self._conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"
                        )
        if version < SCHEMA_VERSION:
            self._conn.execute(
                "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- helpers ----------------------------------------------------------

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def transaction(self):
        """Context manager for an explicit BEGIN/COMMIT/ROLLBACK block."""
        return _Txn(self._lock, self._conn)

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one(
            "SELECT value FROM schema_meta WHERE key=?", (key,)
        )
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # -- backup -----------------------------------------------------------

    def backup_to(self, target: Path) -> None:
        """Consistent snapshot via sqlite3's online backup API."""
        target.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(str(target))
        try:
            with self._lock:
                self._conn.backup(dest)
        finally:
            dest.close()


class _Txn:
    def __init__(self, lock: threading.RLock, conn: sqlite3.Connection):
        self._lock = lock
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute("ROLLBACK")
        finally:
            self._lock.release()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def json_field(row: dict[str, Any], field: str, default: Any = None) -> Any:
    return util.loads(row.get(field) or "", default)

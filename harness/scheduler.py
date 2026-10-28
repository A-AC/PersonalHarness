"""Scheduler and heartbeat: durable timing, normal-prompt submission.

A scheduled task is a durable instruction submitted to the ordinary agent
runtime at its time — the same model, tools, permissions, approvals, and
context construction as an interactive message. The scheduler owns no
execution of its own.

Schedules are ``once``, ``interval``, ``daily``, or ``weekly`` with an
explicit timezone. Occurrence rows keyed by (schedule id, due time) make
submission idempotent across restarts and duplicate ticks. Missed runs
(policy ``run_once`` or ``skip``) are coalesced or skipped explicitly, never
silently duplicated. The heartbeat is the same loop: it checks due schedules,
recovers interrupted work, retries transient model failures after a cooldown,
notifies about pending approvals once, and surfaces paused work — recording
each decision in the event log without flooding the chat view.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from . import util
from .config import Config
from .db import Database, rows_to_dicts
from .store import Events, Tasks

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class Scheduler:
    def __init__(self, app):
        self.app = app
        self.db: Database = app.db
        self.config: Config = app.config
        self.events: Events = app.events
        self.tasks: Tasks = app.tasks
        self.runtime = app.runtime
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tick_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Timezone-aware due-time math
    # ------------------------------------------------------------------

    def _zone(self, tz_name: str):
        import zoneinfo

        try:
            return zoneinfo.ZoneInfo(tz_name)
        except Exception:
            return dt.timezone.utc

    def compute_next_due(
        self, schedule: dict[str, Any], after_utc: dt.datetime
    ) -> str | None:
        """First due time strictly after ``after_utc`` for the schedule."""
        zone = self._zone(schedule["tz"] or "UTC")
        kind = schedule["kind"]
        if kind == "once":
            return None
        if kind == "interval":
            interval = float(schedule["interval_s"] or 0)
            if interval <= 0:
                return None
            step = dt.timedelta(seconds=interval)
            due = after_utc + step
            return util.iso(due)
        local_after = after_utc.astimezone(zone)
        hh, mm = 9, 0
        try:
            parts = (schedule["time_of_day"] or "09:00").split(":")
            hh, mm = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            pass
        if kind == "daily":
            candidate = local_after.replace(
                hour=hh, minute=mm, second=0, microsecond=0
            )
            if candidate <= local_after:
                candidate += dt.timedelta(days=1)
            return util.iso(candidate.astimezone(dt.timezone.utc))
        if kind == "weekly":
            wanted = (schedule["weekday"] or "mon").lower()[:3]
            if wanted not in WEEKDAYS:
                wanted = "mon"
            target_index = WEEKDAYS.index(wanted)
            candidate = local_after.replace(
                hour=hh, minute=mm, second=0, microsecond=0
            )
            days_ahead = (target_index - candidate.weekday()) % 7
            candidate += dt.timedelta(days=days_ahead)
            if candidate <= local_after:
                candidate += dt.timedelta(days=7)
            return util.iso(candidate.astimezone(dt.timezone.utc))
        return None

    # ------------------------------------------------------------------
    # Schedule CRUD
    # ------------------------------------------------------------------

    def create_schedule(
        self,
        *,
        name: str,
        prompt: str,
        kind: str,
        tz: str | None = None,
        at_ts: str | None = None,
        interval_s: float | None = None,
        time_of_day: str | None = None,
        weekday: str | None = None,
        domain_id: str | None = None,
    ) -> dict[str, Any]:
        if kind not in ("once", "interval", "daily", "weekly"):
            raise ValueError(f"unknown schedule kind: {kind}")
        if not prompt.strip() or not name.strip():
            raise ValueError("schedule needs a name and a prompt")
        schedule_id = util.new_id("sch")
        ts = util.iso()
        tz = tz or str(self.config.get("timezone", "UTC"))
        now = util.now_utc()
        schedule = {
            "id": schedule_id, "name": name.strip(), "prompt": prompt.strip(),
            "domain_id": domain_id, "kind": kind, "at_ts": at_ts,
            "interval_s": interval_s, "time_of_day": time_of_day,
            "weekday": weekday, "tz": tz, "enabled": 1,
            "created_at": ts, "updated_at": ts, "next_due_at": None,
        }
        if kind == "once":
            if not at_ts:
                raise ValueError("once schedule needs at_ts")
            schedule["next_due_at"] = util.iso(util.parse_iso(at_ts))
        else:
            schedule["next_due_at"] = self.compute_next_due(schedule, now)
        self.db.execute(
            "INSERT INTO schedules(id, name, prompt, domain_id, kind, at_ts,"
            " interval_s, time_of_day, weekday, tz, enabled, created_at,"
            " updated_at, next_due_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(schedule[key] for key in (
                "id", "name", "prompt", "domain_id", "kind", "at_ts",
                "interval_s", "time_of_day", "weekday", "tz", "enabled",
                "created_at", "updated_at", "next_due_at",
            )),
        )
        self.events.record(
            "schedule.created", schedule_id=schedule_id,
            data={"name": schedule["name"], "kind": kind,
                  "next_due_at": schedule["next_due_at"]},
        )
        return self.get_schedule(schedule_id)  # type: ignore[return-value]

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM schedules WHERE id=?", (schedule_id,)
        )
        return rows_to_dicts([row])[0] if row else None

    def list_schedules(self) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self.db.query("SELECT * FROM schedules ORDER BY created_at")
        )

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        self.db.execute(
            "UPDATE schedules SET enabled=?, updated_at=? WHERE id=?",
            (int(enabled), util.iso(), schedule_id),
        )
        self.events.record(
            "schedule.enabled" if enabled else "schedule.disabled",
            schedule_id=schedule_id,
        )
        return self.get_schedule(schedule_id)  # type: ignore[return-value]

    def delete_schedule(self, schedule_id: str) -> None:
        self.db.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        self.events.record("schedule.deleted", schedule_id=schedule_id)

    # ------------------------------------------------------------------
    # Occurrence submission (idempotent)
    # ------------------------------------------------------------------

    def submit_due(self, now: dt.datetime | None = None) -> list[dict[str, Any]]:
        """Submit work for every due occurrence. Idempotent: an occurrence
        recorded as submitted is never submitted again."""
        now = now or util.now_utc()
        submitted: list[dict[str, Any]] = []
        for schedule in self.list_schedules():
            if not schedule["enabled"] or not schedule["next_due_at"]:
                continue
            due = util.parse_iso(schedule["next_due_at"])
            if due > now:
                continue
            slots = self._missed_slots(schedule, due, now)
            if not slots:
                continue
            policy = str(self.config.get("scheduler.missed_policy", "run_once"))
            if policy == "skip":
                for slot in slots:
                    self._record_occurrence(
                        schedule, slot, "skipped", note="missed while stopped"
                    )
                self._advance(schedule, slots[-1])
                continue
            # run_once: coalesce to the most recent missed slot.
            for slot in slots[:-1]:
                self._record_occurrence(
                    schedule, slot, "skipped", note="missed_coalesced"
                )
            outcome = self._submit_occurrence(schedule, slots[-1])
            if outcome:
                submitted.append(outcome)
            self._advance(schedule, slots[-1])
        return submitted

    def _missed_slots(
        self, schedule: dict[str, Any], first_due: dt.datetime,
        now: dt.datetime,
    ) -> list[dt.datetime]:
        slots = []
        cursor = first_due
        while cursor <= now and len(slots) < 50:
            slots.append(cursor)
            nxt = self.compute_next_due(schedule, cursor)
            if not nxt:
                break
            cursor = util.parse_iso(nxt)
        return slots

    def _advance(self, schedule: dict[str, Any], last_slot: dt.datetime) -> None:
        next_due = self.compute_next_due(schedule, last_slot)
        if schedule["kind"] == "once":
            self.db.execute(
                "UPDATE schedules SET enabled=0, next_due_at=NULL, updated_at=?"
                " WHERE id=?",
                (util.iso(), schedule["id"]),
            )
        else:
            self.db.execute(
                "UPDATE schedules SET next_due_at=?, updated_at=? WHERE id=?",
                (next_due, util.iso(), schedule["id"]),
            )

    def _occurrence_id(self, schedule_id: str, due: dt.datetime) -> str:
        return f"{schedule_id}:{util.iso(due)}"

    def _record_occurrence(
        self, schedule: dict[str, Any], due: dt.datetime, status: str,
        note: str | None = None,
    ) -> str:
        occ_id = self._occurrence_id(schedule["id"], due)
        self.db.execute(
            "INSERT OR IGNORE INTO schedule_occurrences(id, schedule_id,"
            " due_at, status, note) VALUES (?,?,?,?,?)",
            (occ_id, schedule["id"], util.iso(due), status, note),
        )
        return occ_id

    def _submit_occurrence(
        self, schedule: dict[str, Any], due: dt.datetime
    ) -> dict[str, Any] | None:
        """Claim an occurrence and create its task atomically.

        The task row, occurrence status, and task id commit together. A crash
        therefore leaves either nothing or a complete claim, never a task
        plus a still-pending occurrence that the next heartbeat duplicates.
        """
        occ_id = self._occurrence_id(schedule["id"], due)
        due_iso = util.iso(due)
        created = False
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM schedule_occurrences WHERE id=?", (occ_id,)
            ).fetchone()
            if existing and existing["status"] in (
                "submitted", "skipped", "failed",
            ):
                return None

            task_id = existing["task_id"] if existing else None
            task_row = conn.execute(
                "SELECT id, status FROM tasks WHERE id=?", (task_id,)
            ).fetchone() if task_id else None

            # Adopt a task created by an older crash after the task insert
            # but before the occurrence was marked submitted.
            if task_row is None:
                adopted = conn.execute(
                    "SELECT id, status FROM tasks WHERE origin='schedule'"
                    " AND origin_ref=? ORDER BY created_at LIMIT 1",
                    (occ_id,),
                ).fetchone()
                if adopted is not None:
                    task_id = adopted["id"]
                    task_row = adopted

            if task_row is None:
                task_id = self.tasks.create_in_transaction(
                    conn,
                    objective=schedule["prompt"],
                    domain_id=schedule.get("domain_id"),
                    origin="schedule", origin_ref=occ_id,
                    meta={"notes": ""},
                )
                created = True
                task_status = "active"
                has_run = False
            else:
                task_status = task_row["status"]
                has_run = conn.execute(
                    "SELECT id FROM runs WHERE task_id=? LIMIT 1", (task_id,)
                ).fetchone() is not None

            if existing:
                conn.execute(
                    "UPDATE schedule_occurrences SET status='submitted',"
                    " submitted_at=COALESCE(submitted_at, ?), task_id=?"
                    " WHERE id=?",
                    (util.iso(), task_id, occ_id),
                )
            else:
                conn.execute(
                    "INSERT INTO schedule_occurrences(id, schedule_id, due_at,"
                    " status, submitted_at, task_id) VALUES (?,?,?,?,?,?)",
                    (occ_id, schedule["id"], due_iso, "submitted",
                     util.iso(), task_id),
                )

        task = self.tasks.get(task_id) if task_id else None
        if created and task is not None:
            self.events.record(
                "task.created", task_id=task_id,
                domain_id=schedule.get("domain_id"),
                data={"objective": schedule["prompt"][:200],
                      "origin": "schedule"},
            )
        self.events.record(
            "schedule.submitted", schedule_id=schedule["id"],
            task_id=task_id,
            data={"occurrence": occ_id, "prompt": schedule["prompt"][:200]},
        )
        if (
            task is None or has_run
            or task_status in ("completed", "failed", "cancelled")
        ):
            return None
        return {"schedule_id": schedule["id"], "occurrence_id": occ_id,
                "task": task}

    def _run_submitted(self, item: dict[str, Any]) -> None:
        task = item["task"]
        self.runtime.start_run(
            task, {"kind": "schedule", "name": self._schedule_name(item)},
            trigger_text=(
                f"[scheduled: {self._schedule_name(item)}] {task['objective']}"
            ),
        )

    def _schedule_name(self, item: dict[str, Any]) -> str:
        schedule = self.get_schedule(item["schedule_id"])
        return schedule["name"] if schedule else "schedule"

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def heartbeat(self) -> dict[str, Any]:
        """One deterministic pass. Submits work through the normal runtime;
        records what it examined and did. Never generates a chat message
        per check."""
        report: dict[str, Any] = {
            "tick": util.iso(), "submitted": [], "resumed": [],
            "retried": [], "notified": [], "skipped": 0, "deferred": 0,
        }
        if not self._tick_lock.acquire(blocking=False):
            return {"tick": util.iso(), "skipped": "another tick is running"}
        try:
            now = util.now_utc()
            max_runs = max(
                0, int(self.config.get("scheduler.max_runs_per_tick", 1))
            )
            runs_started = 0

            # 0. Collapse duplicate pending approvals before anything looks at
            #    them; identical actions must never queue up per new task.
            self.app.approvals.dedupe_pending()

            # 1. Due schedules (idempotent occurrence submission).
            submitted_task_ids: set[str] = set()
            for item in self.submit_due(now):
                submitted_task_ids.add(item["task"]["id"])
                if max_runs and runs_started >= max_runs:
                    report["deferred"] += 1
                    continue
                self._run_submitted(item)
                runs_started += 1
                report["submitted"].append(item["occurrence_id"])
            # 2. Scheduled tasks submitted but never started (crash gap).
            for task in self.tasks.list_by_status(("active",)):
                if task["origin"] != "schedule":
                    continue
                if task["id"] in submitted_task_ids:
                    continue
                runs = self.db.query_one(
                    "SELECT id FROM runs WHERE task_id=? LIMIT 1", (task["id"],)
                )
                if runs is not None:
                    continue
                if max_runs and runs_started >= max_runs:
                    report["deferred"] += 1
                    continue
                self.runtime.start_run(
                    task, {"kind": "schedule"}, trigger_text=task["objective"]
                )
                runs_started += 1
                report["resumed"].append(task["id"])
            # 3. Approvals granted just before a crash but never claimed.
            for result in self.runtime.resume_approved_approvals():
                if result.status not in ("skipped", "stale"):
                    report["resumed"].append(result.task_id)
            # 4. Interrupted runs: resume once after a restart.
            if bool(self.config.get("scheduler.resume_interrupted", True)):
                for run in rows_to_dicts(self.db.query(
                    "SELECT * FROM runs WHERE status='interrupted'"
                )):
                    if max_runs and runs_started >= max_runs:
                        report["deferred"] += 1
                        continue
                    outcome = self.runtime.resume_interrupted_run(run["id"])
                    if outcome.status == "skipped":
                        # Terminal task or stale debris: the run is closed,
                        # not resumed, and must not consume a run slot.
                        continue
                    runs_started += 1
                    report["resumed"].append(run["task_id"])
            # 5. Retry paused model failures with exponential backoff, bounded.
            report["retried"] = self._retry_paused(now)
            # 6. Expiries.
            self.app.approvals.expire_stale()
            self.app.memory.expire_stale()
            # 7. One-time notifications for pending approvals and paused work.
            report["notified"] = self._notify_pending()
            # 8. Scheduled consolidation.
            self._maybe_consolidate(now)
            # 9. Occurrences left pending by a crash between record/submit.
            self._recover_pending_occurrences(now)
            self.db.meta_set("scheduler.last_tick", util.iso())
            self.db.meta_set("scheduler.last_report", util.dumps(report))
        finally:
            self._tick_lock.release()
        return report

    def _retry_paused(self, now: dt.datetime) -> list[str]:
        """Retry paused model failures after their backoff, at most N/tick."""
        max_retries = int(self.config.get("scheduler.max_task_retries", 3))
        per_tick = max(
            0, int(self.config.get("scheduler.max_retries_per_tick", 1))
        )
        retry_permanent = bool(
            self.config.get("scheduler.retry_permanent", False)
        )
        cooldown = float(self.config.get("scheduler.retry_cooldown_s", 300))
        base = float(self.config.get("scheduler.retry_backoff_base_s", 300))
        cap = float(self.config.get("scheduler.retry_backoff_max_s", 3600))
        grace = float(self.config.get("scheduler.user_activity_grace_s", 60))
        retried: list[str] = []
        for task in self.tasks.list_by_status(("paused",)):
            if per_tick and len(retried) >= per_tick:
                break
            reason = task.get("status_reason") or ""
            permanent = reason.startswith("model protocol failure")
            if not permanent and not reason.startswith("model failure"):
                continue
            if permanent and not retry_permanent:
                continue
            count = int(task.get("retry_count") or 0)
            if count >= max_retries:
                continue
            if self._user_active_since(task, grace, now):
                continue
            due_text = task.get("next_retry_at")
            if not due_text:
                delay = cooldown if count == 0 else min(
                    base * (2 ** max(0, count - 1)), cap
                )
                due = now + dt.timedelta(seconds=max(0.0, delay))
                self.tasks.set_retry(task["id"], util.iso(due))
                if delay > 0:
                    continue
                due_text = util.iso(due)
            if util.parse_iso(due_text) > now:
                continue
            self.tasks.increment_retry(task["id"])
            self.tasks.set_retry(task["id"], None)
            try:
                self.runtime.retry_paused_task(task)
            except Exception as exc:  # one stuck task must not kill the tick
                self.events.record(
                    "retry.failed", level="error", task_id=task["id"],
                    data={"error": f"{type(exc).__name__}: {exc}"},
                )
                continue
            retried.append(task["id"])
        return retried

    def _user_active_since(
        self, task: dict[str, Any], grace: float, now: dt.datetime
    ) -> bool:
        """True when the user has messaged since this task failed.

        Activity *before* the failure is what caused it; only newer activity
        means the user is actively working and a background retry should wait.
        """
        if grace <= 0:
            return False
        recent = self.app.messages.recent(limit=1)
        if not recent:
            return False
        newest = recent[-1]
        if newest.get("role") != "user":
            return False
        last = util.parse_iso(newest["ts"])
        failure = util.parse_iso(task.get("last_activity_at") or "")
        return last > failure and (now - last).total_seconds() < grace

    def _recover_pending_occurrences(self, now: dt.datetime) -> None:
        grace = float(self.config.get("scheduler.missed_grace_s", 900))
        for row in rows_to_dicts(self.db.query(
            "SELECT * FROM schedule_occurrences WHERE status='pending'"
        )):
            schedule = self.get_schedule(row["schedule_id"])
            if schedule is None:
                continue
            due = util.parse_iso(row["due_at"])
            if (now - due).total_seconds() > grace:
                self.db.execute(
                    "UPDATE schedule_occurrences SET status='skipped',"
                    " note='recovered: too old after crash' WHERE id=?",
                    (row["id"],),
                )
                continue
            outcome = self._submit_occurrence(schedule, due)
            if outcome:
                self._run_submitted(outcome)

    def _notify_pending(self) -> list[str]:
        out: list[str] = []
        notify_after = float(self.config.get("scheduler.approval_notify_s", 3600))
        now = util.now_utc()
        for approval in self.app.approvals.pending():
            if approval.get("notified_at"):
                continue
            created = util.parse_iso(approval["created_at"])
            if (now - created).total_seconds() < notify_after:
                continue
            self.db.execute(
                "UPDATE approvals SET notified_at=? WHERE id=?",
                (util.iso(), approval["id"]),
            )
            self.app.messages.record(
                "note",
                f"Reminder: action **{approval['tool']}** is still waiting "
                "for your approval.",
                source="system", task_id=approval["task_id"],
            )
            out.append(approval["id"])
        return out

    def _maybe_consolidate(self, now: dt.datetime) -> None:
        interval = float(self.config.get("consolidation.schedule_interval_s", 0) or 0)
        if interval <= 0:
            return
        last = self.db.meta_get("consolidation.last_run")
        if last and (now - util.parse_iso(last)).total_seconds() < interval:
            return
        self.app.consolidation.run(trigger="scheduled")

    # ------------------------------------------------------------------
    # Threaded operation (server process only)
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        interval = float(self.config.get("scheduler.tick_interval_s", 30))
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="scheduler", daemon=True
        )
        self._thread.start()
        self.events.record("scheduler.started", data={"interval_s": interval})

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
            self.events.record("scheduler.stopped")

    def _loop(self) -> None:
        interval = float(self.config.get("scheduler.tick_interval_s", 30))
        while not self._stop.wait(interval):
            if not bool(self.config.get("scheduler.enabled", True)):
                continue
            try:
                report = self.heartbeat()
                if any(report.get(key) for key in
                       ("submitted", "resumed", "retried", "notified")):
                    self.events.record("scheduler.tick", data=report)
            except Exception as exc:  # a broken tick never kills the thread
                self.events.record(
                    "scheduler.error", level="error",
                    data={"error": f"{type(exc).__name__}: {exc}"},
                )

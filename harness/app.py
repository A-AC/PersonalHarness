"""Composition root: build, wire, recover, lock, back up, shut down.

Everything is constructed here and nowhere else. ``App`` is the single
object the server, CLI-client-facing API, and tests drive. Startup recovery
converts crash debris (running runs, in-flight executions, orphan approvals)
into explicit, inspectable state; the heartbeat then resumes what should
resume. One process may write a data directory: ``serve`` mode holds a
process lock (pid + heartbeat timestamp) and refuses to start beside a live
writer.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Any

from . import util
from .approvals import Approvals
from .config import Config, load_config
from .consolidation import Consolidation
from .context import ContextBuilder
from .db import Database, rows_to_dicts
from .logging import get_logger, setup_logging
from .memory import MemoryManager
from .model import Model, ModelClient, ScriptedModel
from .runtime import AgentRuntime
from .scheduler import Scheduler
from .store import Domains, Events, Messages, Tasks
from .tools import ToolContext, ToolRegistry, McpTools

log = get_logger("app")

LOCK_REFRESH_S = 5.0
LOCK_STALE_S = 25.0


def _pid_alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


class ProcessLock:
    """Best-effort single-writer guard for serve mode."""

    def __init__(self, path: Path):
        self.path = path
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def acquire(self) -> None:
        info = {"pid": os.getpid(), "ts": util.iso()}
        if self.path.exists():
            try:
                old = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                old = None
            if old and isinstance(old, dict):
                age = (util.now_utc() - util.parse_iso(old.get("ts", ""))).total_seconds()
                if age < LOCK_STALE_S and _pid_alive(int(old.get("pid", 0))):
                    raise RuntimeError(
                        f"another harness process (pid {old.get('pid')}) is "
                        "writing this data directory; refusing to start"
                    )
        self._write(info)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._refresh_loop, name="process-lock", daemon=True
        )
        self._thread.start()

    def _write(self, info: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(info), encoding="utf-8")

    def _refresh_loop(self) -> None:
        while not self._stop.wait(LOCK_REFRESH_S):
            try:
                self._write({"pid": os.getpid(), "ts": util.iso()})
            except OSError:
                pass

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        try:
            self.path.unlink()
        except OSError:
            pass


class App:
    def __init__(
        self,
        data_dir: Path,
        config: Config | None = None,
        model: Model | None = None,
    ):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or load_config(data_dir)
        setup_logging(
            data_dir,
            level=str(self.config.get("logging.level", "info")),
        )
        self.db = Database(data_dir / "harness.db")
        self.artifacts_dir = data_dir / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        self.events = Events(self.db)
        self.messages = Messages(self.db)
        self.domains = Domains(self.db, self.events)
        self.tasks = Tasks(self.db, self.events)
        self.memory = MemoryManager(
            self.db, self.events, self.artifacts_dir, self.config
        )
        self.model = model or ModelClient(self.config)
        self.tools = ToolRegistry(
            self.db, self.events, self.config, self.artifacts_dir
        )
        self.mcp = McpTools(self.tools, self.config, self.events)
        self.mcp.load()
        self.approvals = Approvals(self.db, self.events, self.config, self.model)
        self.context = ContextBuilder(
            self.config, self.domains, self.tasks, self.messages,
            self.memory, self.artifacts_dir,
        )
        self.runtime = AgentRuntime(self)
        self.scheduler = Scheduler(self)
        self.consolidation = Consolidation(self)
        self._lock: ProcessLock | None = None
        self.domains.ensure_defaults()

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    def startup(self, *, serve: bool = False) -> None:
        """Recover crash debris, optionally take the writer lock."""
        if serve:
            self._lock = ProcessLock(self.data_dir / "process.lock")
            self._lock.acquire()
        self.recover()
        self.events.record("system.start", data={"serve": serve})

    def shutdown(self) -> None:
        """Graceful shutdown: stop ticking, finish current work, persist."""
        try:
            self.scheduler.stop()
        except Exception:
            pass
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        try:
            self.events.record("system.shutdown")
            self.db.close()
        except Exception:
            pass  # already closed (e.g. after a restore)

    def recover(self) -> dict[str, Any]:
        """Convert crash debris into explicit state. Deterministic, no model.

        - in-flight tool executions become ``unknown`` and are appended to the
          run transcript, so a resumed model cannot mistake them for success;
        - approvals are reconciled: approved-but-unclaimed approvals stay for
          the heartbeat, approved-and-finished ones are consumed, and approvals
          attached to terminal runs are cancelled instead of orphaned;
        - a run with a live approval waits for a decision; other running runs
          are ``interrupted`` and their task paused.
        """
        report = {"unknown_executions": 0, "interrupted_runs": [],
                  "recovered_approvals": 0, "cancelled_approvals": 0}

        # 1. Tool attempts that were mid-flight when the process died.
        for row in rows_to_dicts(self.db.query(
            "SELECT * FROM tool_executions WHERE status='in_flight'"
        )):
            error = "interrupted: outcome unknown (crash or restart)"
            self.db.execute(
                "UPDATE tool_executions SET status='unknown', ended_at=?,"
                " error=? WHERE id=?",
                (util.iso(), error, row["id"]),
            )
            self._append_execution_observation(row, "unknown", error)
            if row.get("approval_id"):
                self.approvals.mark_executed(row["approval_id"])
            report["unknown_executions"] += 1
            self._interrupt_execution_run(row, report)

        # 1b. A final execution can be committed before its tool result is
        # appended to the transcript (crash between record and append). Put
        # the real result back in front of the model so a resume cannot repeat
        # an action whose row already says it happened.
        for row in rows_to_dicts(self.db.query(
            "SELECT e.* FROM tool_executions e JOIN runs r ON r.id=e.run_id"
            " WHERE r.status IN ('running','waiting_approval','interrupted')"
            " AND e.status IN ('succeeded','failed','denied','unknown')"
            " AND e.tool_call_id IS NOT NULL"
        )):
            self._append_execution_observation(
                row, row["status"], row.get("error")
            )

        # 1c. A rejection/cancellation/expiry may have committed just before
        # the process died, before the runtime could deny the awaiting row.
        # Make that outcome explicit before the run can resume.
        for approval in rows_to_dicts(self.db.query(
            "SELECT * FROM approvals WHERE status IN"
            " ('rejected','cancelled','expired')"
        )):
            self._deny_awaiting_execution(
                approval["id"], f"approval {approval['status']}"
            )

        # 2. Approved approvals whose execution already finished, or whose
        # execution record vanished. Awaiting executions are left for the
        # heartbeat to claim and run.
        for approval in rows_to_dicts(self.db.query(
            "SELECT * FROM approvals WHERE status='approved'"
        )):
            execution = rows_to_dicts(self.db.query(
                "SELECT * FROM tool_executions WHERE approval_id=?"
                " ORDER BY started_at DESC LIMIT 1", (approval["id"],)
            ))
            row = execution[0] if execution else None
            if row is None:
                self.approvals.cancel(
                    approval["id"],
                    reason="approved without a matching execution record",
                )
                self._append_missing_approval_observation(
                    approval, "no execution record"
                )
                report["cancelled_approvals"] += 1
                continue
            if row["status"] == "awaiting_approval":
                run = self.db.query_one(
                    "SELECT status FROM runs WHERE id=?",
                    (approval["run_id"],),
                )
                if run is None or run["status"] in (
                    "completed", "failed", "limit_reached", "cancelled",
                ):
                    reason = (
                        "run no longer exists" if run is None
                        else f"run ended as {run['status']}"
                    )
                    self.approvals.cancel(approval["id"], reason=reason)
                    self._deny_awaiting_execution(approval["id"], reason)
                    report["cancelled_approvals"] += 1
                continue  # heartbeat will claim and execute it
            self.approvals.mark_executed(approval["id"])
            self._append_execution_observation(
                row, row["status"], row.get("error")
            )
            run = self.db.query_one(
                "SELECT status FROM runs WHERE id=?", (approval["run_id"],)
            )
            if run and run["status"] in ("running", "waiting_approval"):
                self._mark_interrupted(
                    approval["run_id"], approval["task_id"],
                    "approval execution finished before crash", report,
                )

        # 3. Pending approvals whose run already ended can never execute.
        for approval in rows_to_dicts(self.db.query(
            "SELECT * FROM approvals WHERE status='pending'"
        )):
            execution = self.db.query_one(
                "SELECT id FROM tool_executions WHERE approval_id=? LIMIT 1",
                (approval["id"],),
            )
            if execution is None:
                # The approval row committed but the matching execution row
                # did not (crash between the two inserts). There is nothing to
                # execute later, so cancel it explicitly.
                reason = "pending approval has no execution record"
                self.approvals.cancel(approval["id"], reason=reason)
                self._append_missing_approval_observation(approval, reason)
                report["cancelled_approvals"] += 1
                continue
            run = self.db.query_one(
                "SELECT status FROM runs WHERE id=?", (approval["run_id"],)
            )
            if run is None or run["status"] in (
                "completed", "failed", "limit_reached", "cancelled",
            ):
                reason = (
                    "run no longer exists" if run is None
                    else f"run ended as {run['status']}"
                )
                self.approvals.cancel(approval["id"], reason=reason)
                self._deny_awaiting_execution(approval["id"], reason)
                report["cancelled_approvals"] += 1

        # 4. Runs left in ``running``.
        for run in rows_to_dicts(self.db.query(
            "SELECT * FROM runs WHERE status='running'"
        )):
            live = self.approvals.live_for_run(run["id"])
            if live is not None:
                self.db.execute(
                    "UPDATE runs SET status='waiting_approval' WHERE id=?",
                    (run["id"],),
                )
                self.tasks.set_status(
                    run["task_id"], "waiting_approval",
                    reason="recovered: awaiting approval",
                )
                report["recovered_approvals"] += 1
                continue
            self._mark_interrupted(
                run["id"], run["task_id"], "interrupted by restart", report
            )

        # 5. A run waiting on an approval that no longer exists is stuck.
        for run in rows_to_dicts(self.db.query(
            "SELECT * FROM runs WHERE status='waiting_approval'"
        )):
            if self.approvals.live_for_run(run["id"]) is not None:
                continue
            self._mark_interrupted(
                run["id"], run["task_id"],
                "approval disappeared before execution", report,
            )

        # 5b. A run parked on a user question can still have a live approval
        # (older state or a hand-written row). Move it to the approval state so
        # deciding the action is not permanently stale.
        for run in rows_to_dicts(self.db.query(
            "SELECT * FROM runs WHERE status='waiting_user'"
        )):
            live = self.approvals.live_for_run(run["id"])
            if live is None:
                continue
            self.db.execute(
                "UPDATE runs SET status='waiting_approval', decision=NULL,"
                " ended_at=NULL WHERE id=?",
                (run["id"],),
            )
            self.tasks.set_status(
                run["task_id"], "waiting_approval",
                reason="recovered: awaiting approval",
            )
            report["recovered_approvals"] += 1

        # 6. An interrupted run with a live approval can still be decided.
        for run in rows_to_dicts(self.db.query(
            "SELECT * FROM runs WHERE status='interrupted'"
        )):
            live = self.approvals.live_for_run(run["id"])
            if live is None:
                continue
            self.db.execute(
                "UPDATE runs SET status='waiting_approval', decision=NULL,"
                " ended_at=NULL WHERE id=?",
                (run["id"],),
            )
            self.tasks.set_status(
                run["task_id"], "waiting_approval",
                reason="recovered: awaiting approval",
            )
            report["recovered_approvals"] += 1

        if any(report.values()):
            self.events.record("recovery.run", data=report)
        return report

    def _mark_interrupted(
        self, run_id: str, task_id: str, reason: str,
        report: dict[str, Any],
    ) -> None:
        self.db.execute(
            "UPDATE runs SET status='interrupted', decision=?, ended_at=?"
            " WHERE id=?",
            (reason, util.iso(), run_id),
        )
        self.tasks.set_status(task_id, "paused", reason="interrupted")
        self.events.record(
            "recovery.interrupted_run", level="warn",
            run_id=run_id, task_id=task_id, data={"reason": reason},
        )
        if run_id not in report["interrupted_runs"]:
            report["interrupted_runs"].append(run_id)

    def _interrupt_execution_run(
        self, execution: dict[str, Any], report: dict[str, Any]
    ) -> None:
        run = self.db.query_one(
            "SELECT status FROM runs WHERE id=?", (execution["run_id"],)
        )
        if run and run["status"] in ("running", "waiting_approval"):
            self._mark_interrupted(
                execution["run_id"], execution["task_id"],
                "interrupted during tool execution", report,
            )

    def _append_execution_observation(
        self, execution: dict[str, Any], status: str,
        error: str | None = None,
    ) -> None:
        """Put an explicit tool outcome into the run transcript.

        Recovery runs before any model call, so the next resume sees the
        unknown/finished result instead of repeating an irreversible action.
        """
        row = self.db.query_one(
            "SELECT transcript FROM runs WHERE id=?", (execution["run_id"],)
        )
        if row is None:
            return
        transcript = util.loads(row["transcript"] or "[]", [])
        call_id = execution.get("tool_call_id")
        if call_id and any(
            message.get("tool_call_id") == call_id for message in transcript
        ):
            return
        if status == "unknown":
            body: Any = {
                "status": "unknown",
                "tool": execution.get("tool"),
                "error": error or "outcome unknown",
                "note": "The action may or may not have happened. Check "
                        "before repeating it.",
            }
        elif status == "succeeded":
            body = {
                "status": "succeeded",
                "result": util.loads(execution.get("result") or "null", None),
            }
        else:
            body = {
                "status": status,
                "error": error or execution.get("error") or status,
            }
        has_call = False
        if call_id:
            for message in transcript:
                for call in message.get("tool_calls") or []:
                    if call.get("id") == call_id:
                        has_call = True
                        break
        text = util.clip(util.dumps(body), int(
            self.config.get("limits.max_tool_result_chars", 6000)
        ))
        if call_id and has_call:
            transcript.append({
                "role": "tool", "tool_call_id": call_id, "content": text,
            })
        else:
            transcript.append({
                "role": "system",
                "content": f"[recovery] {execution.get('tool')} {status}: {text}",
            })
        self.db.execute(
            "UPDATE runs SET transcript=? WHERE id=?",
            (util.dumps(transcript), execution["run_id"]),
        )

    def _append_missing_approval_observation(
        self, approval: dict[str, Any], reason: str
    ) -> None:
        self._append_execution_observation(
            {
                "run_id": approval["run_id"],
                "task_id": approval["task_id"],
                "tool": approval["tool"],
                "tool_call_id": approval.get("tool_call_id"),
                "status": "denied",
                "error": reason,
            },
            "denied", reason,
        )

    def _deny_awaiting_execution(
        self, approval_id: str, reason: str
    ) -> None:
        row = self.db.query_one(
            "SELECT * FROM tool_executions WHERE approval_id=? AND"
            " status='awaiting_approval' ORDER BY started_at LIMIT 1",
            (approval_id,),
        )
        if row is None:
            return
        execution = rows_to_dicts([row])[0]
        self.db.execute(
            "UPDATE tool_executions SET status='denied', ended_at=?,"
            " error=? WHERE id=?",
            (util.iso(), reason, execution["id"]),
        )
        self._append_execution_observation(execution, "denied", reason)

    # ------------------------------------------------------------------
    # Helpers used by runtime/scheduler/API
    # ------------------------------------------------------------------

    def tool_context(
        self, run_id: str, task_id: str, domain_id: str | None
    ) -> ToolContext:
        return ToolContext(
            run_id=run_id, task_id=task_id, domain_id=domain_id,
            memory=self.memory, domains=self.domains, tasks=self.tasks,
            messages=self.messages,
        )

    # ------------------------------------------------------------------
    # Backup / restore
    # ------------------------------------------------------------------

    def backup(self, target: Path | None = None) -> Path:
        """Consistent snapshot: DB via the online backup API + artifacts."""
        stamp = util.iso().replace(":", "").replace("-", "")
        target = target or self.data_dir / "backups" / f"harness-{stamp}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_db = target.with_suffix(".tmp.db")
        self.db.backup_to(tmp_db)
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(tmp_db, "harness.db")
                config_path = self.data_dir / "config.yaml"
                if config_path.is_file():
                    zf.write(config_path, "config.yaml")
                if self.artifacts_dir.is_dir():
                    for path in sorted(self.artifacts_dir.rglob("*")):
                        if path.is_file():
                            zf.write(path, str(Path("artifacts") / path.relative_to(self.artifacts_dir)))
        finally:
            tmp_db.unlink(missing_ok=True)
        self.events.record("backup.created", data={"path": str(target)})
        return target

    def restore(self, archive: Path) -> dict[str, Any]:
        """Replace state from a backup. Requires exclusive access; the
        process must restart afterwards so all handles reopen cleanly."""
        if not archive.is_file():
            raise FileNotFoundError(str(archive))
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            if "harness.db" not in names:
                raise ValueError("not a harness backup (missing harness.db)")
            db_path = self.db.path
            self.db.close()
            db_path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)
            zf.extract("harness.db", self.data_dir)
            if "config.yaml" in names:
                zf.extract("config.yaml", self.data_dir)
            if any(n.startswith("artifacts/") for n in names):
                shutil.rmtree(self.artifacts_dir, ignore_errors=True)
                self.artifacts_dir.mkdir(parents=True, exist_ok=True)
                for name in names:
                    if name.startswith("artifacts/") and not name.endswith("/"):
                        rel = name[len("artifacts/"):]
                        dest = self.artifacts_dir / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(name) as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)
        log.info("state restored from %s", archive)
        return {"restored": True, "restart_required": True}


def build_app(
    data_dir: str | Path,
    *,
    environ: dict[str, str] | None = None,
    model: Model | None = None,
) -> App:
    """Test/CLI helper: build an App (recovery NOT run; call startup())."""
    path = Path(data_dir)
    config = load_config(path, environ=environ)
    return App(path, config=config, model=model or ScriptedModel())

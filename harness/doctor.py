"""Preflight diagnostics for a data directory and its environment.

``harness doctor`` runs without the server: it reads the config read-only,
checks the model endpoint, the listening port, the process lock, module
health, and free disk space. Warnings are informational; only ``fail``
checks make the report unsuccessful.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__, util
from .config import load_config

MIN_DISK_BYTES = 100 * 1024 * 1024
WARN_DISK_BYTES = 1024 * 1024 * 1024
LOCK_STALE_S = 25.0


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check that matches the writer-lock rules."""
    if pid <= 0:
        return False
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
    except Exception:
        return False


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _check_python() -> dict[str, str]:
    version = platform.python_version()
    if sys.version_info < (3, 12):
        return _check("python", "fail", f"{version} (need 3.12 or newer)")
    return _check("python", "ok", version)


def _check_data_writable(data_dir: Path) -> dict[str, str]:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        handle, path = tempfile.mkstemp(prefix=".doctor-", dir=str(data_dir))
        os.close(handle)
        os.unlink(path)
    except OSError as exc:
        return _check(
            "data_writable", "fail", f"data directory is not writable: {exc}"
        )
    geteuid = getattr(os, "geteuid", None)
    uid = f"uid {geteuid()}" if callable(geteuid) else "uid unknown"
    return _check(
        "data_writable",
        "ok",
        f"writable ({uid}; the container user is UID 10001)",
    )


def _check_endpoint(config) -> dict[str, str]:
    from .model import ModelClient

    client = ModelClient(config)
    try:
        info = client.list_models()
    except Exception as exc:
        info = {
            "reachable": False,
            "models": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        try:
            client.close()
        except Exception:
            pass
    if not info.get("reachable"):
        detail = str(info.get("error") or "endpoint is unreachable")
        return _check("endpoint", "fail", detail)
    models = [str(name) for name in info.get("models") or []]
    configured = str(config.get("model.name") or "")
    if configured and configured not in models:
        detail = f"reachable but {configured!r} is not in the model list"
        if info.get("error"):
            detail += f" ({info['error']})"
        return _check("endpoint", "warn", detail)
    return _check(
        "endpoint", "ok", f"reachable; {len(models)} model(s) listed"
    )


def _check_port(config) -> dict[str, str]:
    host = str(config.get("server.host") or "127.0.0.1")
    try:
        port = int(config.get("server.port") or 8080)
    except (TypeError, ValueError):
        port = 8080
    target = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    try:
        with socket.create_connection((target, port), timeout=0.5):
            pass
    except OSError:
        return _check("port", "ok", f"nothing is listening on {target}:{port}")
    try:
        with urllib.request.urlopen(
            f"http://{target}:{port}/health", timeout=2.0
        ) as response:
            data = json.loads(response.read(4096).decode() or "{}")
        if (
            getattr(response, "status", 0) == 200
            and isinstance(data, dict)
            and data.get("status") == "ok"
        ):
            return _check("port", "ok", f"running harness on {target}:{port}")
    except Exception:
        pass
    return _check(
        "port",
        "warn",
        "port in use by another service (SearXNG also defaults to 8080)",
    )


def _check_writer_lock(data_dir: Path) -> dict[str, str]:
    path = data_dir / "process.lock"
    if not path.is_file():
        return _check("writer_lock", "ok", "no lock file")
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(info, dict):
            raise ValueError("lock is not an object")
        pid = int(info.get("pid", 0))
        age = (util.now_utc() - util.parse_iso(str(info.get("ts", ""))))
    except Exception as exc:
        return _check(
            "writer_lock",
            "warn",
            f"unreadable lock (will be taken over): {exc}",
        )
    if age.total_seconds() < LOCK_STALE_S and _pid_alive(pid):
        return _check(
            "writer_lock", "ok", f"held by a live process (pid {pid})"
        )
    return _check(
        "writer_lock", "warn", "stale lock (will be taken over)"
    )


def _check_modules(data_dir: Path) -> dict[str, str]:
    db_path = data_dir / "harness.db"
    if not db_path.is_file():
        return _check("modules", "ok", "no database yet")
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            rows = connection.execute(
                "SELECT id, status, last_error FROM modules"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return _check("modules", "warn", f"cannot read modules: {exc}")
    broken = [
        str(row[0]) for row in rows
        if str(row[1] or "") == "error" or row[2]
    ]
    if broken:
        return _check(
            "modules", "warn", "module errors: " + ", ".join(broken[:20])
        )
    return _check("modules", "ok", f"{len(rows)} module(s) healthy")


def _check_disk(data_dir: Path) -> dict[str, str]:
    target = data_dir if data_dir.exists() else data_dir.parent
    try:
        usage = shutil.disk_usage(str(target))
    except OSError as exc:
        return _check("disk", "fail", f"cannot read disk usage: {exc}")
    free = int(usage.free)
    total = int(usage.total)
    detail = (
        f"{free / (1024 ** 3):.1f} GiB free of "
        f"{total / (1024 ** 3):.1f} GiB"
    )
    if free < MIN_DISK_BYTES:
        return _check("disk", "fail", detail)
    if free < WARN_DISK_BYTES:
        return _check("disk", "warn", detail)
    return _check("disk", "ok", detail)


def run_doctor(data_dir: Path) -> dict[str, Any]:
    """Run every preflight check. Never requires a running server."""
    directory = Path(data_dir)
    checks = [
        _check_python(),
        _check_data_writable(directory),
    ]
    config = load_config(directory, create=False)
    checks.extend([
        _check_endpoint(config),
        _check_port(config),
        _check_writer_lock(directory),
        _check_modules(directory),
        _check_disk(directory),
    ])
    return {
        "ok": all(check["status"] != "fail" for check in checks),
        "version": __version__,
        "data_dir": str(directory),
        "checks": checks,
    }


def format_report(report: dict[str, Any]) -> str:
    """Human-readable doctor output, one line per check plus an overall."""
    labels = {"ok": " ok ", "warn": "warn", "fail": "FAIL"}
    lines = [
        f"[{labels.get(check['status'], check['status'])}] "
        f"{check['name']}: {check['detail']}"
        for check in report.get("checks", [])
    ]
    lines.append(f"overall: {'ok' if report.get('ok') else 'FAIL'}")
    return "\n".join(lines)

from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def now_ts() -> str:
    # Microsecond precision keeps ordering deterministic for records written in
    # the same second (duplicate/conflict resolution and recency tie-breaks).
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    token = secrets.token_hex(8)
    ident = f"{prefix}_{token}"
    if not SAFE_ID.match(ident):
        raise ValueError(f"unsafe id: {ident}")
    return ident


def assert_safe_filename(name: str) -> None:
    stem = name.split(".")[0].lower()
    if ":" in name:
        raise ValueError(f"unsafe filename: {name}")
    if stem in RESERVED_NAMES:
        raise ValueError(f"unsafe filename: {name}")
    if not SAFE_ID.match(name.replace(".", "_")):
        raise ValueError(f"unsafe filename: {name}")


def atomic_write_text(path: Path, text: str, *, retries: int, retry_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_safe_filename(path.name)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    attempts = max(1, retries)
    delay = retry_s
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(delay)
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    raise last_error or OSError("atomic replace failed")


def atomic_write_json(path: Path, data: Any, *, retries: int, retry_s: float) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, payload, retries=retries, retry_s=retry_s)


def read_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def append_jsonl(path: Path, record: dict[str, Any], *, retries: int, retry_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    # Append is not a full rewrite; retry open/write on lock.
    attempts = max(1, retries)
    delay = retry_s
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
            return
        except OSError as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(delay)
    raise last_error or OSError("jsonl append failed")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows

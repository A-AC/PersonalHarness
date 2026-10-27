"""Allocation and safety checks for isolated evaluation data directories.

Evaluation runs write real harness state. They must never touch the deployed
data directory. A sandbox is recognizable by a marker file written here; an
existing non-empty directory without that marker is refused instead of being
reused or cleared.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from harness.storage.atomic import now_ts


MARKER = ".harness-eval-sandbox"
MARKER_FORMAT = "harness-eval-sandbox"
MARKER_VERSION = 1


class UnsafeDataDirectory(ValueError):
    """The requested evaluation directory is not an isolated sandbox."""


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _protected_paths() -> set[Path]:
    protected = {
        Path.home().resolve(),
        Path.cwd().resolve(),
        Path(tempfile.gettempdir()).resolve(),
        (Path.cwd() / "data").resolve(),
    }
    # A Windows drive root or POSIX root is never an evaluation sandbox.
    anchor = Path.cwd().anchor
    if anchor:
        protected.add(Path(anchor).resolve())
    return {item for item in protected if str(item)}


def prepare_sandbox(
    path: str | Path,
    *,
    allow_existing_unmarked: bool = False,
) -> Path:
    """Validate or create a sandbox root and return its resolved path.

    ``allow_existing_unmarked`` exists for tests that intentionally create an
    empty temporary directory before calling the evaluator. Even then, a
    non-empty directory without the marker is refused.
    """

    root = Path(path).expanduser()
    try:
        root = root.resolve()
    except OSError as exc:
        raise UnsafeDataDirectory(f"cannot resolve evaluation directory {path!r}: {exc}") from exc

    if root in _protected_paths():
        raise UnsafeDataDirectory(
            f"refusing to evaluate in protected directory {root}; use a dedicated sandbox"
        )
    cwd = Path.cwd().resolve()
    if root == cwd or _is_within(cwd, root):
        raise UnsafeDataDirectory(
            f"refusing to evaluate in {root}; the sandbox must be a child directory, not a parent"
        )

    marker = root / MARKER
    if root.exists():
        if not root.is_dir():
            raise UnsafeDataDirectory(f"evaluation path is not a directory: {root}")
        if marker.exists():
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise UnsafeDataDirectory(f"invalid sandbox marker at {marker}: {exc}") from exc
            if payload.get("format") != MARKER_FORMAT:
                raise UnsafeDataDirectory(f"unrecognized sandbox marker at {marker}")
            return root
        entries = [item for item in root.iterdir() if item.name not in {".", ".."}]
        if entries and not allow_existing_unmarked:
            raise UnsafeDataDirectory(
                f"refusing to reuse non-empty directory {root} without a {MARKER} marker; "
                "choose a dedicated sandbox path"
            )
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "format": MARKER_FORMAT,
                "version": MARKER_VERSION,
                "created_at": now_ts(),
                "owner_pid": os.getpid(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return root


def default_sandbox_root() -> Path:
    """A safe default outside the repository and outside the production data dir."""

    base = Path(tempfile.gettempdir()) / "harness-eval"
    return base

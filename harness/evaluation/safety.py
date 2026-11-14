"""Sandbox preparation and protection for evaluation runs.

Every scenario/repeat writes into its own directory under a sandbox root. The
root carries a marker file, so an unrelated non-empty directory is never
cleared by mistake. The repository and the deployed ``data/`` directory are
always refused.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

MARKER = ".harness-eval-sandbox"


class SandboxError(RuntimeError):
    pass


def default_sandbox_root() -> Path:
    return Path(tempfile.gettempdir()) / "harness-eval"


def _protected(repo_root: Path, data_dir: Path | None) -> list[Path]:
    protected = [repo_root.resolve()]
    if data_dir is not None:
        protected.append(Path(data_dir).resolve())
    protected.append((repo_root / "data").resolve())
    return protected


def prepare_sandbox(
    root: Path | str | None = None,
    *,
    repo_root: Path | None = None,
    data_dir: Path | str | None = None,
    reset: bool = False,
) -> Path:
    """Resolve and validate the sandbox root, creating the marker.

    ``reset`` removes a previous evaluation root (only when it carries our
    marker). Without it, previous run directories are left alone.
    """
    repo = (repo_root or Path(__file__).resolve().parents[2]).resolve()
    target = Path(root or default_sandbox_root()).expanduser().resolve()
    for protected in _protected(repo, Path(data_dir) if data_dir else None):
        # The sandbox may not be, live inside, or contain a protected path.
        if (
            target == protected
            or protected in target.parents
            or target in protected.parents
        ):
            raise SandboxError(
                f"refusing to use a protected path as a sandbox: {target}"
            )
    if target.exists() and not target.is_dir():
        raise SandboxError(f"sandbox path is not a directory: {target}")

    marker = target / MARKER
    if target.exists() and any(target.iterdir()) and not marker.exists():
        if reset:
            raise SandboxError(
                f"refusing to clear an unmarked non-empty directory: {target}"
            )
        raise SandboxError(
            f"sandbox directory is non-empty and not marked: {target} "
            f"(add {MARKER} or choose another --sandbox)"
        )
    if reset and target.exists() and marker.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    marker.write_text("harness evaluation sandbox\n", encoding="utf-8")
    return target


def scenario_dir(
    sandbox_root: Path, run_id: str, scenario_id: str, repeat: int
) -> Path:
    safe = "".join(
        char if char.isalnum() or char in "-_." else "_"
        for char in scenario_id
    )
    path = sandbox_root / "runs" / run_id / f"{safe}_r{repeat}"
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    (path / "data").mkdir(parents=True, exist_ok=True)
    return path / "data"

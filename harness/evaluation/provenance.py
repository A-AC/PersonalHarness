"""Source and scenario identity for reproducible evaluation artifacts."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from .. import __version__, git_sha, util


def _fingerprint(parts: Iterable[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(parts):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def code_fingerprint() -> str:
    root = Path(__file__).resolve().parents[2]
    return _fingerprint(
        (str(path.relative_to(root)).replace("\\", "/"), path.read_bytes())
        for base in (root / "harness", root / "scripts")
        for path in base.rglob("*.py")
    )


def scenario_fingerprint(scenarios: Iterable[Any]) -> str:
    payload = util.dumps([
        scenario.as_dict() if hasattr(scenario, "as_dict") else scenario
        for scenario in scenarios
    ]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def source_metadata(scenarios: Iterable[Any] = ()) -> dict[str, Any]:
    selected = list(scenarios)
    return {
        "version": __version__,
        "git_sha": git_sha(),
        "code_fingerprint": code_fingerprint(),
        "scenario_fingerprint": scenario_fingerprint(selected),
    }

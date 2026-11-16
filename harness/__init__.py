"""Personal assistant harness.

A compact, persistent, inspectable personal assistant. One model endpoint,
one agent runtime, one durable interaction history, one authoritative state
store (SQLite). The model proposes; the harness validates, executes, records,
and decides.
"""

from __future__ import annotations

import os

__version__ = "2.0.0"


def git_sha() -> str:
    """The commit this build came from, baked in at image build time.

    Read from ``HARNESS_GIT_SHA`` (set by the Dockerfile build arg); never
    queries git at runtime. Returns ``"unknown"`` when not baked in.
    """
    value = os.environ.get("HARNESS_GIT_SHA", "").strip()
    return value or "unknown"

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class TurnTimeline:
    """Wall-clock stage recorder for one user turn.

    Stages are stored as plain dictionaries so they serialize directly into a
    trace file and can be rendered by the web UI without another contract.
    ``on_stage`` lets a client observe stages while the turn is still running.
    """

    on_stage: Callable[[dict[str, Any]], None] | None = None
    started: float = field(default_factory=time.monotonic)
    stages: list[dict[str, Any]] = field(default_factory=list)

    def stage(self, name: str, *, duration_ms: float, detail: Any = None, start: float | None = None) -> None:
        item: dict[str, Any] = {
            "stage": name,
            "start_ms": max(0, int(((start if start is not None else time.monotonic()) - self.started) * 1000)),
            "duration_ms": max(0, int(duration_ms)),
        }
        if detail is not None:
            item["detail"] = detail
        self.stages.append(item)
        if self.on_stage is not None:
            try:
                self.on_stage(dict(item))
            except Exception:
                # Observability must never break a turn.
                pass

    def record(self, name: str, started: float, detail: Any = None) -> None:
        self.stage(name, duration_ms=(time.monotonic() - started) * 1000, detail=detail, start=started)

    def begin(self, name: str, detail: Any = None) -> None:
        """Tell observers a stage is starting; do not store it in the trace."""
        if self.on_stage is None:
            return
        item: dict[str, Any] = {
            "stage": name,
            "start_ms": max(0, int((time.monotonic() - self.started) * 1000)),
            "duration_ms": 0,
            "running": True,
        }
        if detail is not None:
            item["detail"] = detail
        try:
            self.on_stage(item)
        except Exception:
            pass

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.stages]

    def total_ms(self) -> int:
        return max(0, int((time.monotonic() - self.started) * 1000))

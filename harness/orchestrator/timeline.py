from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnTimeline:
    """Wall-clock stage recorder for one user turn.

    Stages are stored as plain dictionaries so they serialize directly into a
    trace file and can be rendered by the web UI without another contract.
    """

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

    def record(self, name: str, started: float, detail: Any = None) -> None:
        self.stage(name, duration_ms=(time.monotonic() - started) * 1000, detail=detail, start=started)

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.stages]

    def total_ms(self) -> int:
        return max(0, int((time.monotonic() - self.started) * 1000))

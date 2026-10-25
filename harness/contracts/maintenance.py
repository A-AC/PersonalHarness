from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class RemovedState:
    """One state path removed by a reset."""

    path: str
    files: int
    bytes: int


@dataclass
class ResetReport:
    scope: str
    removed: list[RemovedState] = field(default_factory=list)
    backup_path: str | None = None
    seeded: list[str] = field(default_factory=list)
    cleared_references: int = 0
    duration_ms: int = 0


class ResetService(Protocol):
    """Wipes learned state and leaves a valid, seeded harness behind."""

    def run(
        self,
        scope: str = "full",
        *,
        actor: str = "user",
        backup: bool | None = None,
    ) -> ResetReport: ...

    def scopes(self) -> tuple[str, ...]: ...

    @property
    def busy(self) -> bool: ...

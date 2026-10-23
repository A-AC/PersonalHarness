from __future__ import annotations

from typing import Protocol


class SourceResolver(Protocol):
    def excerpt(self, ref: str) -> str | None: ...

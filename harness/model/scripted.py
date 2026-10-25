from __future__ import annotations

import json
from typing import Any

from harness.contracts.model import Message, ModelResult, ModelUnavailable


class ScriptedAdapter:
    """Deterministic adapter: each complete() pops the next scripted payload."""

    def __init__(self, script: list[Any] | None = None, *, healthy: bool = True):
        self._script = list(script or [])
        self.healthy = healthy

    def push(self, item: Any) -> None:
        self._script.append(item)

    def complete(
        self,
        role: str,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ModelResult:
        if not self._script:
            raise ModelUnavailable("scripted adapter exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        if item == "UNAVAILABLE":
            raise ModelUnavailable("scripted unavailable")
        if isinstance(item, dict):
            content = json.dumps(item, ensure_ascii=False)
        else:
            content = str(item)
        if on_delta is not None:
            on_delta(content)
        return ModelResult(
            content=content,
            latency_ms=1,
            prompt_tokens=len(content),
            completion_tokens=8,
            time_to_first_token_ms=1,
        )

    def health(self, role: str) -> bool:
        return self.healthy

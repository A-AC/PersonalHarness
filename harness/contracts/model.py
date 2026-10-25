from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class ModelError(RuntimeError):
    """Base error for inference adapter failures."""


class ModelUnavailable(ModelError):
    """The inference backend could not be reached or returned no usable result."""


@dataclass
class Message:
    role: str
    content: str


@dataclass
class ModelResult:
    content: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    time_to_first_token_ms: int | None = None
    reasoning_content: str | None = None
    timings: dict[str, Any] = field(default_factory=dict)
    streaming: bool = False


class ModelAdapter(Protocol):
    def complete(
        self,
        role: str,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ModelResult: ...

    def health(self, role: str) -> bool: ...

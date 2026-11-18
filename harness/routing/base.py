"""Router value objects shared by every routing strategy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class RouterError(RuntimeError):
    """Infrastructure failure: the caller may fall back to lexical routing."""


@dataclass
class RoutingRequest:
    text: str
    task: dict[str, Any] | None = None
    task_domain: str | None = None
    recent_user_turns: list[str] = field(default_factory=list)
    domains: list[dict[str, Any]] = field(default_factory=list)
    trusted: bool = True
    source: str = "web"
    trigger: dict[str, Any] | None = None
    task_meta: dict[str, Any] | None = None


@dataclass
class RouterResult:
    domain_id: str
    strategy: str
    confidence: str = "low"
    reason: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    score: float = 0.0
    breakdown: dict[str, Any] = field(default_factory=dict)
    matched: list[str] = field(default_factory=list)
    hint: dict[str, Any] | None = None
    latency_ms: float = 0.0
    inputs: dict[str, Any] = field(default_factory=dict)
    fallback: str | None = None
    shadow: list[dict[str, Any]] = field(default_factory=list)

    def as_routing(self) -> dict[str, Any]:
        return {
            "domain_id": self.domain_id,
            "score": self.score,
            "reason": self.reason,
            "candidates": self.candidates,
            "breakdown": self.breakdown,
            "matched": self.matched,
            "hint": self.hint,
            "strategy": self.strategy,
            "confidence": self.confidence,
            "latency_ms": self.latency_ms,
            "inputs": self.inputs,
            "fallback": self.fallback,
            "shadow": self.shadow,
        }


class Router(Protocol):
    name: str

    def route(self, request: RoutingRequest) -> RouterResult: ...

"""The unchanged keyword router, wrapped as the control strategy."""

from __future__ import annotations

import time
from typing import Any

from .. import util
from .base import RouterResult, RoutingRequest


def _learned_affinity(app, text: str) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    sources = (
        app.memory.domain_affinity(text),
        app.modules.skills.domain_affinity(text),
    )
    for source in sources:
        for domain_id, terms in source.items():
            bucket = merged.setdefault(str(domain_id), [])
            for term in terms:
                if term not in bucket:
                    bucket.append(term)
    return merged


def _confidence(routing: dict[str, Any]) -> str:
    candidates = routing.get("candidates") or []
    specific = [
        item for item in candidates
        if str(item.get("domain_id") or "") != "general"
    ]
    if not specific:
        return "low"
    if str(routing.get("domain_id") or "") == "general":
        return "low"
    score = float(specific[0].get("score") or 0.0)
    runner = float(specific[1].get("score") or 0.0) if len(specific) > 1 else 0.0
    if score - runner >= 0.75:
        return "high"
    if score - runner >= 0.25:
        return "medium"
    return "low"


class LexicalRouter:
    name = "lexical"

    def __init__(self, app):
        self.app = app

    def route(self, request: RoutingRequest) -> RouterResult:
        started = time.monotonic()
        routing = self.app.domains.route(
            request.text,
            open_tasks=self.app.tasks.list_open(),
            recent_messages=self.app.messages.recent(limit=10),
            learned=_learned_affinity(self.app, request.text),
        )
        latency = (time.monotonic() - started) * 1000.0
        return RouterResult(
            domain_id=str(routing.get("domain_id") or "general"),
            strategy=self.name,
            confidence=_confidence(routing),
            reason=str(routing.get("reason") or ""),
            candidates=list(routing.get("candidates") or []),
            score=float(routing.get("score") or 0.0),
            breakdown=routing.get("breakdown") or {},
            matched=list(routing.get("matched") or []),
            hint=routing.get("hint"),
            latency_ms=round(latency, 2),
            inputs={
                "message": util.clip(request.text, 500),
                "recent_turns": len(request.recent_user_turns),
                "domain_count": len(request.domains),
                "trusted": request.trusted,
            },
        )

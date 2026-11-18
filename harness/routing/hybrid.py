"""Embedding top-3, then an LLM pick when the margin is narrow."""

from __future__ import annotations

import time
from typing import Any

from .base import RouterResult, RoutingRequest
from .embedding import EmbeddingRouter
from .llm import LLMRouter


class HybridRouter:
    name = "hybrid"

    def __init__(self, app, embedding_router=None, llm_router=None):
        self.app = app
        self.embedding = embedding_router or EmbeddingRouter(app)
        self.llm = llm_router or LLMRouter(app)

    def route(self, request: RoutingRequest) -> RouterResult:
        started = time.monotonic()
        embedding = self.embedding.route(request)
        candidates = list(embedding.candidates or [])[:3]
        margin = float(self.app.config.get("routing.hybrid_margin", 0.15))
        if embedding.domain_id != "general" and len(candidates) <= 1:
            embedding.strategy = self.name
            return embedding
        if len(candidates) >= 2:
            gap = float(candidates[0].get("score") or 0.0) - float(
                candidates[1].get("score") or 0.0
            )
            if gap >= margin:
                embedding.strategy = self.name
                embedding.reason = (
                    f"embedding margin {gap:.3f} >= {margin:.3f}; "
                    + embedding.reason
                )
                embedding.latency_ms = round(
                    (time.monotonic() - started) * 1000.0, 2
                )
                return embedding
        allowed = {
            str(card.get("domain_id") or "") for card in candidates
        }
        narrowed = RoutingRequest(
            text=request.text,
            task=request.task,
            task_domain=request.task_domain,
            recent_user_turns=request.recent_user_turns,
            domains=[
                domain for domain in request.domains
                if str(domain.get("id") or "") in allowed
            ],
            trusted=request.trusted,
            source=request.source,
            trigger=request.trigger,
            task_meta=request.task_meta,
        )
        picked = self.llm.route(narrowed)
        picked.strategy = self.name
        picked.reason = "embedding margin narrow; " + picked.reason
        picked.latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        picked.candidates = candidates
        return picked

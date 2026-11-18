"""Strategy selection, fallback, and shadow evaluation for domain routing."""

from __future__ import annotations

import time
from typing import Any

from .. import util
from .base import RouterError, RouterResult, RoutingRequest
from .embedding import EmbeddingRouter
from .hybrid import HybridRouter
from .lexical import LexicalRouter
from .llm import LLMRouter


class RoutingService:
    def __init__(self, app, *, embedding_client=None, llm_classifier=None):
        self.app = app
        self.embedding_client = embedding_client
        self.llm_classifier = llm_classifier
        self._routers = {}

    def _router(self, name: str):
        if name in self._routers:
            return self._routers[name]
        if name == "lexical":
            router = LexicalRouter(self.app)
        elif name == "embedding":
            router = EmbeddingRouter(self.app, client=self.embedding_client)
        elif name == "llm":
            router = LLMRouter(self.app, classifier=self.llm_classifier)
        elif name == "hybrid":
            embedding = EmbeddingRouter(self.app, client=self.embedding_client)
            llm = LLMRouter(self.app, classifier=self.llm_classifier)
            router = HybridRouter(self.app, embedding, llm)
        else:
            raise RouterError(f"unknown routing strategy: {name}")
        self._routers[name] = router
        return router

    def _recent_turns(self, text: str) -> list[str]:
        limit = int(self.app.config.get("routing.recent_turns", 2) or 0)
        if limit <= 0:
            return []
        turns = [
            str(message.get("content") or "")
            for message in self.app.messages.recent(limit=20)
            if message.get("role") == "user"
            and str(message.get("content") or "").strip()
            and str(message.get("content") or "").strip() != text.strip()
        ]
        return turns[-limit:]

    def _request(
        self,
        text: str,
        *,
        task: dict[str, Any] | None,
        source: str,
        trigger: dict[str, Any] | None,
        task_meta: dict[str, Any] | None,
        trusted: bool,
    ) -> RoutingRequest:
        return RoutingRequest(
            text=text,
            task=task,
            task_domain=str((task or {}).get("domain_id") or "") or None,
            recent_user_turns=self._recent_turns(text),
            domains=self.app.domains.list_active(),
            trusted=trusted,
            source=source,
            trigger=trigger,
            task_meta=task_meta,
        )

    def route(
        self,
        text: str,
        *,
        task: dict[str, Any] | None = None,
        source: str = "web",
        trigger: dict[str, Any] | None = None,
        task_meta: dict[str, Any] | None = None,
        trusted: bool = True,
    ) -> RouterResult:
        request = self._request(
            text, task=task, source=source, trigger=trigger,
            task_meta=task_meta, trusted=trusted,
        )
        configured = str(
            self.app.config.get("routing.strategy", "lexical") or "lexical"
        ).strip().lower()
        strategy = configured if trusted else "lexical"
        started = time.monotonic()
        try:
            result = self._router(strategy).route(request)
        except Exception as exc:
            reason = (
                "untrusted source keeps lexical routing"
                if not trusted else f"{type(exc).__name__}: {exc}"
            )
            lexical = self._router("lexical").route(request)
            lexical.fallback = "lexical"
            lexical.reason = f"{reason}; lexical fallback: {lexical.reason}"
            lexical.latency_ms = round((time.monotonic() - started) * 1000.0, 2)
            self.app.events.record(
                "router.fallback", source="routing",
                data={
                    "strategy": strategy,
                    "fallback": "lexical",
                    "reason": util.clip(reason, 400),
                    "domain_id": lexical.domain_id,
                },
            )
            result = lexical
        if result.fallback and result.fallback != "lexical":
            self.app.events.record(
                "router.fallback", source="routing",
                data={
                    "strategy": result.strategy,
                    "fallback": result.domain_id,
                    "reason": util.clip(result.fallback, 200),
                    "domain_id": result.domain_id,
                },
            )
        shadows = self._shadow(
            request, result, configured, trusted
        )
        result.shadow = shadows
        result.inputs = {
            **(result.inputs or {}),
            "strategy": result.strategy,
            "configured_strategy": configured,
        }
        return result

    def _shadow(
        self,
        request: RoutingRequest,
        primary: RouterResult,
        configured: str,
        trusted: bool,
    ) -> list[dict[str, Any]]:
        shadows = self.app.config.get("routing.shadow_strategies", []) or []
        if not isinstance(shadows, list) or not trusted:
            return []
        out: list[dict[str, Any]] = []
        for name in shadows:
            name = str(name or "").strip().lower()
            if not name or name == configured or name == primary.strategy:
                continue
            started = time.monotonic()
            try:
                result = self._router(name).route(request)
                out.append({
                    "strategy": name,
                    "domain_id": result.domain_id,
                    "confidence": result.confidence,
                    "reason": util.clip(result.reason, 300),
                    "latency_ms": result.latency_ms,
                    "fallback": result.fallback,
                })
            except Exception as exc:
                out.append({
                    "strategy": name,
                    "error": util.clip(f"{type(exc).__name__}: {exc}", 300),
                    "latency_ms": round(
                        (time.monotonic() - started) * 1000.0, 2
                    ),
                })
        return out

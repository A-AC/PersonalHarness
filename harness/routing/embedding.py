"""Embedding-based domain routing over an OpenAI-compatible endpoint."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import time
import urllib.error
import urllib.request
from typing import Any

from .. import util
from .base import RouterError, RouterResult, RoutingRequest
from .cards import domain_cards


class EmbeddingClient:
    """Small stdlib client plus a packed-float32 SQLite cache."""

    def __init__(self, config, db):
        self.config = config
        self.db = db

    @property
    def model(self) -> str:
        return str(self.config.get("embeddings.model", "") or "")

    def _key(self, text: str) -> str:
        endpoint = str(self.config.get("embeddings.base_url", "") or "").rstrip("/")
        return hashlib.sha256(
            f"{endpoint}\n{self.model}\n{text}".encode("utf-8")
        ).hexdigest()

    def _load(self, keys: list[str]) -> dict[str, list[float]]:
        if not keys:
            return {}
        marks = ",".join("?" for _ in keys)
        rows = self.db.query(
            f"SELECT key, vector FROM embedding_cache WHERE key IN ({marks})",
            tuple(keys),
        )
        out: dict[str, list[float]] = {}
        for row in rows:
            blob = row["vector"]
            if not isinstance(blob, (bytes, bytearray)):
                continue
            count = len(blob) // 4
            if count:
                out[str(row["key"])] = list(
                    struct.unpack("<" + "f" * count, blob)
                )
        return out

    def _store(self, key: str, vector: list[float]) -> None:
        blob = struct.pack("<" + "f" * len(vector), *vector)
        self.db.execute(
            "INSERT OR REPLACE INTO embedding_cache(key, model, dimension,"
            " vector, created_at) VALUES (?,?,?,?,?)",
            (key, self.model, len(vector), blob, util.iso()),
        )

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not bool(self.config.get("embeddings.enabled", False)):
            raise RouterError("embeddings are disabled")
        base_url = str(self.config.get("embeddings.base_url", "") or "").strip()
        if not base_url or not self.model:
            raise RouterError("embeddings.base_url and embeddings.model are required")
        keys = [self._key(text) for text in texts]
        cached = self._load(keys)
        missing: list[tuple[str, str]] = []
        for key, text in zip(keys, texts):
            if key not in cached:
                missing.append((key, text))
        if missing:
            vectors = self._request([text for _, text in missing])
            if len(vectors) != len(missing):
                raise RouterError("embedding response count did not match input")
            for (key, _), vector in zip(missing, vectors):
                cached[key] = vector
                self._store(key, vector)
        return [cached[key] for key in keys]

    def _request(self, texts: list[str]) -> list[list[float]]:
        base = str(self.config.get("embeddings.base_url", "")).rstrip("/")
        path = str(self.config.get("embeddings.path", "/v1/embeddings"))
        url = base + (path if path.startswith("/") else "/" + path)
        payload = json.dumps(
            {"model": self.model, "input": texts}, ensure_ascii=False
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = float(self.config.get("embeddings.timeout_s", 10.0))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise RouterError(f"embedding request failed: {exc}") from exc
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise RouterError("embedding response has no data list")
        vectors: list[list[float]] = []
        for item in data:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list) or not vector:
                raise RouterError("embedding response contained an empty vector")
            try:
                vectors.append([float(value) for value in vector])
            except (TypeError, ValueError) as exc:
                raise RouterError("embedding vector was not numeric") from exc
        return vectors


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _mean(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    size = len(vectors[0])
    return [
        sum(vector[index] for vector in vectors) / len(vectors)
        for index in range(size)
    ]


class EmbeddingRouter:
    name = "embedding"

    def __init__(self, app, client=None):
        self.app = app
        self.client = client or EmbeddingClient(app.config, app.db)

    def route(self, request: RoutingRequest) -> RouterResult:
        started = time.monotonic()
        cards = domain_cards(request.domains)
        if not cards:
            return RouterResult(
                domain_id="general", strategy=self.name,
                reason="no active domains", confidence="low",
                latency_ms=round((time.monotonic() - started) * 1000.0, 2),
            )
        parts = list(request.recent_user_turns[-2:]) + [request.text]
        query_text = "\n".join(part for part in parts if part.strip())
        card_texts: list[str] = []
        card_vectors_inputs: list[list[str]] = []
        for card in cards:
            texts = [card["text"]]
            if bool(self.app.config.get("embeddings.card_examples", True)):
                texts.extend(card["examples"])
            card_vectors_inputs.append(texts)
            card_texts.extend(texts)
        query_vector = self.client.encode([query_text])[0]
        card_vectors = self.client.encode(card_texts)
        index = 0
        cards_scored: list[dict[str, Any]] = []
        for card, texts in zip(cards, card_vectors_inputs):
            vectors = card_vectors[index:index + len(texts)]
            index += len(texts)
            vector = _mean(vectors)
            score = _cosine(query_vector, vector)
            cards_scored.append({
                "domain_id": card["domain_id"],
                "name": card["name"],
                "score": round(score, 6),
            })
        cards_scored.sort(key=lambda item: (-item["score"], item["domain_id"]))
        top = cards_scored[:3]
        best = top[0]
        minimum = float(self.app.config.get("embeddings.min_similarity", 0.2))
        chosen = best["domain_id"] if best["score"] >= minimum else "general"
        margin = best["score"] - (
            top[1]["score"] if len(top) > 1 else 0.0
        )
        confidence = (
            "high" if margin >= 0.12 else
            "medium" if margin >= 0.04 else "low"
        )
        latency = (time.monotonic() - started) * 1000.0
        return RouterResult(
            domain_id=chosen,
            strategy=self.name,
            confidence=confidence,
            reason=(
                f"cosine {best['score']:.3f} against {best['name']}"
                if chosen != "general" else
                f"best cosine {best['score']:.3f} below {minimum:.3f}"
            ),
            candidates=top,
            score=float(best["score"]),
            latency_ms=round(latency, 2),
            inputs={
                "message": util.clip(request.text, 500),
                "recent_turns": len(request.recent_user_turns[-2:]),
                "domain_count": len(cards),
                "embedding_model": getattr(self.client, "model", ""),
            },
        )

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from harness.contracts.model import Message, ModelError, ModelResult, ModelUnavailable
from harness.contracts.settings import SettingsService

__all__ = ["ModelError", "ModelUnavailable", "OpenAICompatAdapter"]


class OpenAICompatAdapter:
    def __init__(self, settings: SettingsService):
        self._settings = settings

    def _role_key(self, role: str, name: str) -> str:
        return f"models.{role}.{name}"

    def complete(
        self,
        role: str,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None = None,
    ) -> ModelResult:
        endpoint = str(self._settings.get(self._role_key(role, "endpoint")))
        retries = int(self._settings.get(self._role_key(role, "retries")))
        timeout = float(self._settings.get(self._role_key(role, "timeout_s")))
        payload = self._payload(role, messages, schema)
        stream = bool(self._settings.get(self._role_key(role, "stream")))
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if stream:
                    return self._stream(endpoint, payload, timeout)
                return self._post(endpoint, payload, timeout)
            except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                if stream:
                    # A backend may reject stream_options or streaming itself.
                    # One non-streaming fallback keeps the adapter portable.
                    stream = False
                    try:
                        return self._post(endpoint, payload, timeout)
                    except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError, ValueError) as fallback:
                        last_error = fallback
                if attempt >= retries:
                    break
        raise ModelUnavailable(str(last_error) if last_error else "model unavailable")

    def health(self, role: str) -> bool:
        endpoint = str(self._settings.get(self._role_key(role, "endpoint")))
        timeout = min(
            float(self._settings.get("models.health_timeout_s")),
            float(self._settings.get(self._role_key(role, "timeout_s"))),
        )
        try:
            # llama.cpp exposes /v1/models adjacent to chat completions
            health_url = endpoint.rsplit("/", 2)[0] + "/models"
            response = httpx.get(health_url, timeout=timeout)
            return response.status_code < 500
        except httpx.HTTPError:
            return False

    def _post(self, endpoint: str, payload: dict[str, Any], timeout: float) -> ModelResult:
        started = time.monotonic()
        response = httpx.post(endpoint, json=payload, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        choice = body["choices"][0]
        message = choice.get("message") or {}
        usage = body.get("usage") or {}
        timings = body.get("timings") or {}
        return ModelResult(
            content=message.get("content") or "",
            latency_ms=int((time.monotonic() - started) * 1000),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            finish_reason=choice.get("finish_reason"),
            raw=body,
            time_to_first_token_ms=self._ttft_from_timings(timings),
            reasoning_content=message.get("reasoning_content") or message.get("reasoning") or None,
            timings=timings,
            streaming=False,
        )

    def _stream(self, endpoint: str, payload: dict[str, Any], timeout: float) -> ModelResult:
        started = time.monotonic()
        first_chunk: float | None = None
        content: list[str] = []
        reasoning: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, Any] = {}
        timings: dict[str, Any] = {}
        body = dict(payload)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        with httpx.stream("POST", endpoint, json=body, timeout=timeout) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if first_chunk is None:
                    first_chunk = time.monotonic()
                if chunk.get("usage"):
                    usage = chunk["usage"]
                if chunk.get("timings"):
                    timings = chunk["timings"]
                for choice in chunk.get("choices") or []:
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        content.append(piece)
                    thought = delta.get("reasoning_content") or delta.get("reasoning")
                    if thought:
                        reasoning.append(thought)
        duration_ms = int((time.monotonic() - started) * 1000)
        return ModelResult(
            content="".join(content),
            latency_ms=duration_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            finish_reason=finish_reason,
            raw={"usage": usage, "timings": timings},
            time_to_first_token_ms=int((first_chunk - started) * 1000) if first_chunk is not None else None,
            reasoning_content="".join(reasoning) or None,
            timings=timings,
            streaming=True,
        )

    def _ttft_from_timings(self, timings: dict[str, Any]) -> int | None:
        """Approximate first-token time when the transport is not streamed."""
        if not timings:
            return None
        prompt_ms = float(timings.get("prompt_ms") or 0.0)
        per_token_ms = float(timings.get("predicted_per_token_ms") or 0.0)
        if not prompt_ms and not per_token_ms:
            return None
        return int(prompt_ms + per_token_ms)

    def _payload(self, role: str, messages: list[Message], schema: dict[str, Any] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._settings.get(self._role_key(role, "model_name")),
            "messages": [{"role": item.role, "content": item.content} for item in messages],
            "temperature": self._settings.get(self._role_key(role, "temperature")),
            "max_tokens": self._settings.get(self._role_key(role, "max_tokens")),
        }
        seed = self._settings.get(self._role_key(role, "seed"))
        if seed is not None:
            payload["seed"] = seed
        budget = self._settings.get(self._role_key(role, "thinking_budget_tokens"))
        if budget is not None:
            payload["thinking_budget_tokens"] = budget
        if not bool(self._settings.get(self._role_key(role, "enable_thinking"))):
            # Qwen3.x chat templates expose thinking through this kwarg. The
            # llama-server build used by the harness honours it directly.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        fmt = self._settings.get(self._role_key(role, "response_format"))
        if fmt == "json_schema" and schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "harness_payload", "schema": schema},
            }
        else:
            payload["response_format"] = {"type": "json_object"}
        return payload

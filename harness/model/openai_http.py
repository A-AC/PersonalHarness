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
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            started = time.monotonic()
            try:
                response = httpx.post(endpoint, json=payload, timeout=timeout)
                response.raise_for_status()
                body = response.json()
                choice = body["choices"][0]
                content = choice["message"].get("content") or ""
                usage = body.get("usage") or {}
                return ModelResult(
                    content=content,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    finish_reason=choice.get("finish_reason"),
                    raw=body,
                )
            except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as exc:
                last_error = exc
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
        fmt = self._settings.get(self._role_key(role, "response_format"))
        if fmt == "json_schema" and schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "harness_payload", "schema": schema},
            }
        else:
            payload["response_format"] = {"type": "json_object"}
        return payload

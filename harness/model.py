"""The model backend: one OpenAI-compatible endpoint, native tool calls.

Verified against the configured llama.cpp server (see README): the endpoint
supports the OpenAI chat-completions tool-calling convention. Some other
OpenAI-compatible servers render tool requests inside content instead; the
client parses that convention too so the runtime tolerates both. Malformed
responses, connect failures, and 5xx are retried a bounded number of times;
4xx client errors are permanent. The harness never falls back to another
model or provider.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from . import util
from .config import Config

# Markers used by servers that embed tool requests in content. Built by
# concatenation so the literal sequences never need to appear in this file.
TOOL_CALL_OPEN = "<" + "tool_call>"
TOOL_CALL_CLOSE = "</" + "tool_call>"

import re

_HERMES_RE = re.compile(
    re.escape(TOOL_CALL_OPEN) + r"\s*(\{.*?\})\s*" + re.escape(TOOL_CALL_CLOSE),
    re.DOTALL,
)

# The waiting-for-user marker the runtime instructs the model to emit when it
# needs information before it can continue (see prompts in context.py).
ASK_USER_TAG = "<" + "ask_user" + "/>"


class ModelUnavailable(RuntimeError):
    """Endpoint unreachable or persistently erroring (retryable upstream)."""


class ModelResponseError(RuntimeError):
    """Permanent protocol problem (malformed/invalid response, 4xx)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str

    @classmethod
    def from_openai(cls, spec: dict[str, Any], fallback_id: str) -> "ToolCall":
        function = spec.get("function") or {}
        raw = function.get("arguments") or "{}"
        if not isinstance(raw, dict):
            try:
                args = json.loads(raw)
            except (ValueError, TypeError):
                args = {}
        else:
            args = raw
        if not isinstance(args, dict):
            args = {}
        return cls(
            id=str(spec.get("id") or fallback_id),
            name=str(function.get("name") or ""),
            arguments=args,
            raw_arguments=raw if isinstance(raw, str) else json.dumps(args),
        )


@dataclass
class ModelReply:
    content: str
    reasoning: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def parse_hermes_tool_calls(content: str) -> list[ToolCall]:
    """Extract tool requests embedded in content (non-native servers)."""
    calls: list[ToolCall] = []
    for index, match in enumerate(_HERMES_RE.finditer(content or "")):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        name = data.get("name")
        if not name:
            continue
        args = data.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        calls.append(
            ToolCall(
                id=f"hermes_{index}_{util.new_id('tc')}",
                name=str(name),
                arguments=args if isinstance(args, dict) else {},
                raw_arguments=json.dumps(args),
            )
        )
    return calls


class Model(Protocol):
    """The runtime depends on this narrow interface (test seam)."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> ModelReply: ...

    def probe(self) -> dict[str, Any]: ...


class ModelClient:
    """HTTP client for the configured endpoint."""

    def __init__(self, config: Config, transport: httpx.BaseTransport | None = None):
        self.config = config
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                float(config.get("model.timeout_s", 180.0)),
                connect=10.0,
            ),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # -- endpoints --------------------------------------------------------

    def _chat_url(self) -> str:
        base = str(self.config.get("model.base_url", "")).rstrip("/")
        path = str(self.config.get("model.chat_path", "/v1/chat/completions"))
        return base + path

    def _models_url(self) -> str:
        base = str(self.config.get("model.base_url", "")).rstrip("/")
        path = str(self.config.get("model.models_path", "/v1/models"))
        return base + path

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = str(self.config.get("model.api_key", "") or "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.get("model.name", ""),
            "messages": messages,
            "max_tokens": int(
                max_tokens or self.config.get("model.max_tokens", 2048)
            ),
        }
        temperature = self.config.get("model.temperature")
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if not bool(self.config.get("model.enable_thinking", True)):
            # Qwen3.x chat templates expose thinking through this kwarg.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    # -- main entry -------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> ModelReply:
        url = self._chat_url()
        payload = self._payload(messages, tools, max_tokens)
        retries = max(0, int(self.config.get("model.connect_retries", 2)))
        backoff = float(self.config.get("model.retry_backoff_s", 2.0))
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            started = time.monotonic()
            try:
                response = self._client.post(
                    url, json=payload, headers=self._headers()
                )
                duration_ms = int((time.monotonic() - started) * 1000)
                if response.status_code >= 500:
                    raise ModelUnavailable(
                        f"endpoint returned {response.status_code}: "
                        f"{response.text[:200]}"
                    )
                if response.status_code >= 400:
                    # Client errors (bad model name, malformed request) are
                    # permanent; do not retry them.
                    raise ModelResponseError(
                        f"endpoint returned {response.status_code}: "
                        f"{response.text[:300]}"
                    )
                return self._parse(response.json(), duration_ms)
            except ModelResponseError:
                raise
            except (
                ModelUnavailable, httpx.HTTPError, json.JSONDecodeError,
                ValueError, KeyError,
            ) as exc:
                last_error = exc
                if attempt >= retries:
                    break
                time.sleep(backoff * (attempt + 1))
        raise ModelUnavailable(str(last_error) if last_error else "model unavailable")

    def _parse(self, body: dict[str, Any], duration_ms: int) -> ModelReply:
        choices = body.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise ModelResponseError("response contained no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            raise ModelResponseError("response message was not an object")
        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ModelResponseError("response content was not a string")
        usage = body.get("usage") or {}
        raw_calls = message.get("tool_calls") or []
        calls = [
            ToolCall.from_openai(spec, fallback_id=util.new_id("tc"))
            for spec in raw_calls
            if isinstance(spec, dict)
        ]
        finish_reason = choice.get("finish_reason")
        if not calls and finish_reason != "tool_calls":
            # Hermes-style servers put tool requests in content.
            hermes = parse_hermes_tool_calls(content)
            if hermes:
                calls = hermes
                content = _HERMES_RE.sub("", content).strip()
        if not content.strip() and not calls:
            # Empty or whitespace-only replies (thinking models sometimes
            # close without content) are malformed, not answers.
            raise ModelResponseError(
                "model returned neither content nor tool calls "
                f"(finish_reason={finish_reason!r})"
            )
        return ModelReply(
            content=content,
            reasoning=message.get("reasoning_content") or message.get("reasoning") or None,
            tool_calls=calls,
            finish_reason=finish_reason,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            latency_ms=duration_ms,
            raw=body,
        )

    # -- diagnostics ------------------------------------------------------

    def probe(self) -> dict[str, Any]:
        """Check connectivity and the configured model without side effects."""
        result: dict[str, Any] = {
            "base_url": self.config.get("model.base_url"),
            "model": self.config.get("model.name"),
            "reachable": False,
            "model_listed": None,
            "chat_ok": False,
            "tool_call_ok": False,
            "detail": "",
        }
        try:
            response = self._client.get(
                self._models_url(), headers=self._headers()
            )
            result["reachable"] = response.status_code < 500
            if response.status_code == 200:
                data = (response.json() or {}).get("data") or []
                ids = [item.get("id") for item in data if isinstance(item, dict)]
                result["model_listed"] = self.config.get("model.name") in ids
                result["models"] = ids
        except (httpx.HTTPError, ValueError) as exc:
            result["detail"] = f"models probe failed: {exc}"
            return result
        try:
            reply = self.chat(
                [{"role": "user", "content": "Reply with the single word: ready"}],
                tools=[],
                # Thinking models need headroom before visible content.
                max_tokens=1024,
            )
            result["chat_ok"] = bool(
                (reply.content or "").strip() or (reply.reasoning or "").strip()
            )
            result["chat_sample"] = (reply.content or "").strip()[:80]
        except (ModelUnavailable, ModelResponseError) as exc:
            result["detail"] = f"chat probe failed: {exc}"
            return result
        try:
            probe_tool = {
                "type": "function",
                "function": {
                    "name": "probe_echo",
                    "description": "Reply with the exact text given",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            }
            reply = self.chat(
                [
                    {"role": "user", "content": "Call the probe_echo tool with text 'hi'."},
                ],
                tools=[probe_tool],
                max_tokens=512,
            )
            result["tool_call_ok"] = bool(reply.tool_calls)
        except (ModelUnavailable, ModelResponseError) as exc:
            result["detail"] = f"tool probe failed: {exc}"
        return result


class ScriptedModel:
    """Deterministic model for tests. Never touches the network.

    Scripts are consumed in order; each entry is either a string (plain
    content reply) or a dict with any of:

    - content: plain reply text
    - tool_calls: list of name/arguments dicts
    - error: raises ModelUnavailable with this message
    - malformed: raises ModelResponseError
    """

    def __init__(
        self,
        scripts: list[dict[str, Any] | str] | None = None,
        default: str = "",
    ):
        self.scripts: list[dict[str, Any] | str] = list(scripts or [])
        self.default = default
        self.calls: list[dict[str, Any]] = []

    def _next(self) -> dict[str, Any] | str:
        if self.scripts:
            return self.scripts.pop(0)
        return self.default

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> ModelReply:
        self.calls.append({"messages": messages, "tools": tools})
        script = self._next()
        if isinstance(script, str):
            script = {"content": script}
        if script.get("error"):
            raise ModelUnavailable(str(script["error"]))
        if script.get("malformed"):
            raise ModelResponseError(str(script.get("detail", "malformed response")))
        calls = [
            ToolCall(
                id=f"call_{index}_{util.new_id('tc')}",
                name=spec["name"],
                arguments=spec.get("arguments", {}),
                raw_arguments=json.dumps(spec.get("arguments", {})),
            )
            for index, spec in enumerate(script.get("tool_calls", []))
        ]
        content = str(script.get("content", ""))
        finish = "tool_calls" if calls else "stop"
        return ModelReply(
            content=content,
            reasoning=script.get("reasoning"),
            tool_calls=calls,
            finish_reason=script.get("finish_reason", finish),
            prompt_tokens=None,
            completion_tokens=None,
            latency_ms=0,
        )

    def probe(self) -> dict[str, Any]:
        return {
            "reachable": True,
            "model_listed": True,
            "chat_ok": True,
            "tool_call_ok": True,
            "detail": "scripted",
        }

    @property
    def last_messages(self) -> list[dict[str, Any]]:
        return self.calls[-1]["messages"] if self.calls else []

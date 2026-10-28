"""Model client: parsing, error classes, retries, Hermes fallback."""

from __future__ import annotations

import json

import httpx
import pytest

from harness.config import load_config
from harness.model import (
    ModelClient,
    ModelResponseError,
    ModelUnavailable,
    parse_hermes_tool_calls,
)

OPEN = "<" + "tool_call>"
CLOSE = "</" + "tool_call>"


def _client(tmp_path, handler, base_url="http://model.test") -> ModelClient:
    config = load_config(tmp_path)
    config.set("model.base_url", base_url)
    transport = httpx.MockTransport(handler)
    return ModelClient(config, transport=transport)


def _ok_body(content="", tool_calls=None, finish="stop"):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
        finish = "tool_calls"
    return {
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_native_tool_call_parsing(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body(tool_calls=[{
            "id": "call_1", "type": "function",
            "function": {"name": "remember",
                         "arguments": "{\"content\": \"hi\"}"},
        }]))

    client = _client(tmp_path, handler)
    reply = client.chat([{"role": "user", "content": "x"}], tools=[{"t": 1}])
    assert reply.tool_calls[0].name == "remember"
    assert reply.tool_calls[0].arguments == {"content": "hi"}
    assert reply.finish_reason == "tool_calls"
    payload = seen["payload"]
    assert payload["tools"] == [{"t": 1}]
    assert payload["tool_choice"] == "auto"
    client.close()


def test_hermes_content_tool_calls(tmp_path):
    body = _ok_body(content=f'{OPEN}{{"name": "echo", "arguments": {{"text": "hi"}}}}{CLOSE} Some words.')
    client = _client(tmp_path, lambda r: httpx.Response(200, json=body))
    reply = client.chat([{"role": "user", "content": "x"}], tools=[])
    assert reply.tool_calls[0].name == "echo"
    assert "Some words." in reply.content
    assert OPEN not in reply.content
    client.close()


def test_parse_hermes_direct():
    calls = parse_hermes_tool_calls(f'{OPEN}{{"name": "a"}}{CLOSE} junk')
    assert calls[0].name == "a"


def test_malformed_response_raises(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"choices": []})

    client = _client(tmp_path, handler)
    with pytest.raises(ModelResponseError):
        client.chat([{"role": "user", "content": "x"}], tools=[])
    client.close()


def test_empty_reply_is_malformed(tmp_path):
    body = {"choices": [{"message": {"role": "assistant", "content": ""},
                          "finish_reason": "length"}]}
    client = _client(tmp_path, lambda r: httpx.Response(200, json=body))
    with pytest.raises(ModelResponseError):
        client.chat([{"role": "user", "content": "x"}], tools=[])
    client.close()


def test_4xx_is_permanent_not_retried(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "bad model"}})

    client = _client(tmp_path, handler)
    with pytest.raises(ModelResponseError):
        client.chat([{"role": "user", "content": "x"}], tools=[])
    assert calls["n"] == 1
    client.close()


def test_5xx_and_connect_errors_retry_then_raise(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={})

    client = _client(tmp_path, handler)
    client.config.set("model.retry_backoff_s", 0)
    with pytest.raises(ModelUnavailable):
        client.chat([{"role": "user", "content": "x"}], tools=[])
    assert calls["n"] == 3  # initial attempt + connect_retries=2
    client.close()


def test_reasoning_content_kept_out_of_visible_content(tmp_path):
    body = {
        "choices": [{
            "message": {"role": "assistant", "content": "Answer.",
                        "reasoning_content": "thinking..."},
            "finish_reason": "stop",
        }]
    }
    client = _client(tmp_path, lambda r: httpx.Response(200, json=body))
    reply = client.chat([{"role": "user", "content": "x"}], tools=[])
    assert reply.content == "Answer."
    assert reply.reasoning == "thinking..."
    client.close()


def test_thinking_disabled_sends_template_kwarg(tmp_path):
    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body(content="ok"))

    client = _client(tmp_path, handler)
    client.config.set("model.enable_thinking", False)
    client.chat([{"role": "user", "content": "x"}], tools=[])
    assert seen["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    client.close()

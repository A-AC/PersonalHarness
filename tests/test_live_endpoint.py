"""H. Real model endpoint integration tests.

These call the configured endpoint at http://192.168.50.40:8181/ and are
skipped by default; run them explicitly with:

    python -m pytest tests/test_live_endpoint.py -m live -o addopts=""

The endpoint must be reachable for these to run; a skip is an environment
condition, not a pass.
"""

from __future__ import annotations

import json

import pytest

from harness.config import load_config

pytestmark = pytest.mark.live
ENDPOINT = "http://192.168.50.40:8181"


def _live_client(tmp_path):
    from harness.model import ModelClient

    config = load_config(tmp_path)
    config.set("model.base_url", ENDPOINT)
    config.set("model.name", "qwen3.5-9b")
    return ModelClient(config)


def test_endpoint_lists_configured_model(tmp_path):
    client = _live_client(tmp_path)
    try:
        result = client.probe()
        assert result["reachable"], result["detail"]
        assert result["model_listed"] is True
        assert "qwen3.5-9b" in result.get("models", [])
    finally:
        client.close()


def test_endpoint_plain_completion(tmp_path):
    client = _live_client(tmp_path)
    try:
        reply = client.chat(
            [{"role": "user", "content":
              "Reply with exactly the word: banana"}],
            tools=[], max_tokens=256,
        )
        assert reply.content.strip()
        assert "banana" in reply.content.lower()
    finally:
        client.close()


def test_endpoint_native_tool_call_protocol(tmp_path):
    tools = [{
        "type": "function",
        "function": {
            "name": "probe_echo",
            "description": "Return the text argument unchanged",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }]
    client = _live_client(tmp_path)
    try:
        reply = client.chat(
            [{"role": "user",
              "content": "Call the probe_echo tool with text 'toolcheck'."}],
            tools=tools, max_tokens=1024,
        )
        assert reply.tool_calls, "endpoint did not return a native tool call"
        call = reply.tool_calls[0]
        assert call.name == "probe_echo"
        assert json.loads(call.raw_arguments).get("text") == "toolcheck"
    finally:
        client.close()


def test_endpoint_multi_step_tool_round_trip(tmp_path):
    tools = [{
        "type": "function",
        "function": {
            "name": "probe_add",
            "description": "Add two integers a and b",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "integer"},
                               "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
        },
    }]
    client = _live_client(tmp_path)
    try:
        first = client.chat(
            [{"role": "user",
              "content": "What is 17 plus 25? Use the probe_add tool."}],
            tools=tools, max_tokens=1024,
        )
        assert first.tool_calls
        call = first.tool_calls[0]
        args = call.arguments if isinstance(call.arguments, dict) else {}
        result = {"result": int(args.get("a", 0)) + int(args.get("b", 0))}
        messages = [
            {"role": "user",
             "content": "What is 17 plus 25? Use the probe_add tool."},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": call.id, "type": "function",
                             "function": {"name": call.name,
                                          "arguments": call.raw_arguments}}]},
            {"role": "tool", "tool_call_id": call.id,
             "content": json.dumps(result)},
        ]
        second = client.chat(messages, tools=tools, max_tokens=512)
        assert "42" in second.content
    finally:
        client.close()


def test_live_end_to_end_turn(tmp_path):
    """One full harness turn against the real endpoint."""
    from harness.app import App
    from harness.config import load_config

    app = App(tmp_path, config=load_config(tmp_path))  # real ModelClient
    app.startup()
    try:
        result = app.runtime.submit_user_message(
            "Say hello and tell me what day type it is (weekday or weekend) "
            "in one short sentence.", source="web",
        )
        assert result.status == "completed"
        assert result.message.strip()
    finally:
        app.shutdown()

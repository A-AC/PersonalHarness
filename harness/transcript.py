"""Transcript hygiene: keep every model request protocol-valid and bounded.

Chat templates are stricter than the OpenAI wire format suggests. In
particular, the Qwen3.x template rejects a ``system`` message that is not the
first message, and it rejects an assistant ``tool_calls`` message that is not
followed by a ``tool`` result. A crash, a recovery observation, or a side
question while an approval is pending can all create those shapes.

This module repairs a transcript before every model call and compacts it to a
token budget. Repairs are explicit (returned to the caller so they can be
logged); nothing here invents a successful tool outcome.
"""

from __future__ import annotations

import json
from typing import Any

from . import util

_PLACEHOLDER = {
    "status": "unknown",
    "error": "no result was recorded for this tool call; do not assume it happened.",
}


def estimate_tokens(
    messages: list[dict[str, Any]],
    tools_schema: list[dict[str, Any]] | None = None,
) -> int:
    """Rough prompt-token estimate (the harness has no tokenizer)."""
    try:
        text = json.dumps(messages, ensure_ascii=False, default=str)
        if tools_schema:
            text += json.dumps(tools_schema, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(messages)
    return util.token_len(text)


def _tool_result_text(messages: list[dict[str, Any]], index: int) -> str:
    return str(messages[index].get("content") or "")


def repair(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return a protocol-valid transcript plus a list of repairs performed.

    Guarantees:
    - the first message is a system message and no later message is a system;
    - every assistant tool call is followed by exactly one matching tool result;
    - orphan tool results become user-role observations;
    - empty assistant messages are dropped.
    """
    repairs: list[str] = []
    source = [dict(message) for message in messages if isinstance(message, dict)]
    if not source:
        return (
            [{"role": "system", "content": "You are a helpful assistant."}],
            ["empty transcript: inserted system message"],
        )
    if source[0].get("role") == "system":
        system = source[0]
        body = source[1:]
    else:
        system = {"role": "system", "content": "You are a helpful assistant."}
        body = source
        repairs.append("prepended system message")

    normalized: list[dict[str, Any]] = []
    for message in body:
        if message.get("role") == "system":
            normalized.append({
                "role": "user",
                "content": "[system note] " + str(message.get("content") or ""),
            })
            repairs.append("converted mid-transcript system message to user")
        else:
            normalized.append(message)

    first_result: dict[str, int] = {}
    for index, message in enumerate(normalized):
        if message.get("role") == "tool" and message.get("tool_call_id"):
            first_result.setdefault(str(message["tool_call_id"]), index)

    consumed: set[int] = set()
    out: list[dict[str, Any]] = [system]
    for index, message in enumerate(normalized):
        if index in consumed:
            continue
        role = message.get("role")
        if role == "assistant":
            calls = message.get("tool_calls") or []
            if not calls and not str(message.get("content") or "").strip():
                repairs.append("dropped empty assistant message")
                continue
            out.append(message)
            for call in calls:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "")
                result_index = first_result.get(call_id) if call_id else None
                if result_index is not None and result_index not in consumed:
                    out.append(normalized[result_index])
                    consumed.add(result_index)
                else:
                    out.append({
                        "role": "tool",
                        "tool_call_id": call_id or "missing_call_id",
                        "content": json.dumps(_PLACEHOLDER),
                    })
                    repairs.append(
                        f"inserted placeholder result for {call_id or 'a call'}"
                    )
        elif role == "tool":
            if index in consumed:
                continue
            out.append({
                "role": "user",
                "content": "[tool result without a matching call] "
                           + _tool_result_text(normalized, index),
            })
            repairs.append("converted orphan tool result to user")
        else:
            out.append(message)
    return out, repairs


def _clip_middle(text: str, limit: int) -> str:
    """Keep the beginning and end of a long system prompt."""
    if len(text) <= limit:
        return text
    limit = max(400, limit)
    head = int(limit * 0.55)
    tail = max(200, limit - head - 40)
    return (
        text[:head]
        + "\n…[context trimmed]…\n"
        + text[-tail:]
    )


def compact(
    messages: list[dict[str, Any]],
    max_prompt_tokens: int,
    tools_schema: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop old turns, then clip tool results and the system prompt to fit.

    The current turn (the last user message and everything after it) is always
    preserved. The system message is preserved at both ends so persona/rules
    and current state survive trimming.
    """
    repairs: list[str] = []
    if max_prompt_tokens <= 0:
        return messages, ["no prompt budget configured"]
    if estimate_tokens(messages, tools_schema) <= max_prompt_tokens:
        return messages, []

    work = [dict(message) for message in messages]
    system = work[0] if work and work[0].get("role") == "system" else {
        "role": "system", "content": "You are a helpful assistant."
    }
    body = work[1:] if work and work[0].get("role") == "system" else work

    last_user = -1
    for index, message in enumerate(body):
        if message.get("role") == "user":
            last_user = index
    if last_user >= 0:
        head, tail = body[:last_user], body[last_user:]
    else:
        head, tail = body[:-6], body[-6:]

    dropped = 0
    while head and estimate_tokens([system] + head + tail, tools_schema) > max_prompt_tokens:
        head.pop(0)
        dropped += 1
    if dropped:
        repairs.append(f"dropped {dropped} older transcript message(s)")

    # Tool results are the most compressible part of a long turn.
    if estimate_tokens([system] + head + tail, tools_schema) > max_prompt_tokens:
        clipped = 0
        for message in tail:
            if message.get("role") != "tool":
                continue
            content = str(message.get("content") or "")
            if len(content) > 1200:
                message["content"] = util.clip(content, 1200)
                clipped += 1
        if clipped:
            repairs.append(f"clipped {clipped} tool result(s)")

    if estimate_tokens([system] + head + tail, tools_schema) > max_prompt_tokens:
        tail_tokens = estimate_tokens(tail, tools_schema)
        budget_chars = max(800, (max_prompt_tokens - tail_tokens - 200) * 4)
        original = str(system.get("content") or "")
        clipped_system = _clip_middle(original, budget_chars)
        if clipped_system != original:
            system = dict(system)
            system["content"] = clipped_system
            repairs.append("clipped system prompt")

    result = [system] + head + tail
    if estimate_tokens(result, tools_schema) > max_prompt_tokens:
        # Last resort: keep only the system prompt and the last two messages.
        result = [system] + tail[-2:]
        repairs.append("kept only the current turn")
    return result, repairs

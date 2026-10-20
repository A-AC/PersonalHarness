
import json
import os
import time
import uuid
from datetime import datetime, timezone

import requests


LLM_URL = os.getenv(
    "LLM_URL", "http://127.0.0.1:8181/v1/chat/completions"
)
LAYA_URL = os.getenv(
    "LAYA_URL", "http://127.0.0.1:8001/v1/systemone"
)
LLM_MODEL = os.getenv("LLM_MODEL", "local-main")
MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "4"))
TRACE_FILE = os.getenv("TRACE_FILE", "events.jsonl")

HTTP = requests.Session()

VALID_KINDS = {"respond", "ask", "act", "continue", "end"}
REGISTERED_ACTIONS = {"get_status", "echo"}


def log(event, **fields):
    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    print("\n[TRACE] " + json.dumps(record, ensure_ascii=False))
    with open(TRACE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main_llm(messages):
    """Ask llama.cpp for one structured proposal."""
    response = HTTP.post(
        LLM_URL,
        json={
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 1024,
            "thinking_budget_tokens": 256,
            "response_format": {"type": "json_object"},
        },
        timeout=180,
    )
    response.raise_for_status()

    content = response.json()["choices"][0]["message"]["content"]
    if not content:
        raise ValueError("Main model returned empty message content.")

    try:
        proposal = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Main model did not return valid JSON: {content}"
        ) from exc

    if not isinstance(proposal, dict):
        raise ValueError("Main model response must be a JSON object.")

    if proposal.get("kind") not in VALID_KINDS:
        raise ValueError(
            f"Unknown proposal kind: {proposal.get('kind')}"
        )

    if not isinstance(proposal.get("message", ""), str):
        raise ValueError("Proposal message must be a string.")

    if not isinstance(proposal.get("action", ""), str):
        raise ValueError("Proposal action must be a string.")

    if not isinstance(proposal.get("args", {}), dict):
        raise ValueError("Proposal args must be a JSON object.")

    return proposal


def laya_route(user_text, proposal, tool_results):
    """Ask Laya to classify the proposed next step."""
    state = {
        "user_request": user_text[:1800],
        "main_model_proposal": json.dumps(
           proposal, ensure_ascii=False
        )[:1800],
        "completed_tool_results": json.dumps(
            tool_results, ensure_ascii=False
        )[-1800:],
    }

    payload = {
        "state": state,
        "questions": {
            "route": {
                "type": "choice",
                "instructions": "Classify the main assistant's proposed next step. The main_model_proposal is the primary source for this decision. Use user_request and completed_tool_results only as supporting context. If the proposal's kind is 'act' and its action field names a registered tool, choose act. If its kind is 'ask', choose ask. If its kind is 'respond', choose respond. If its kind is 'continue', choose continue. If its kind is 'end', choose end. Do not override the proposal's kind based on the user's original intent or past actions. This is a routing classification, not authorization to execute an action.",
                "criteria": {
                    "respond": (
                        "The current proposal provides the user with "
                        "a final conversational answer. No further "
                        "action or clarification is needed."
                    ),
                    "ask": (
                        "The current proposal asks the user for missing "
                        "information or clarification before proceeding."
                    ),
                    "act": (
                        "The current proposal explicitly requests a "
                        "registered tool action. A past action or a "
                        "mention of a tool is not enough."
                    ),
                    "continue": (
                        "The current proposal explicitly indicates that "
                        "another reasoning iteration is needed."
                    ),
                    "end": (
                        "The current proposal explicitly ends the "
                        "interaction without another response."
                    ),
                },
            }
        },
    }

    response = HTTP.post(LAYA_URL, json=payload, timeout=120)
    response.raise_for_status()
    result = response.json()

    answer = result.get("answers", {}).get("route", {})
    route = answer.get("choice")

    if route not in VALID_KINDS:
        raise ValueError(
            "Unexpected Laya response: "
            + json.dumps(result, ensure_ascii=False)
        )

    return route, result


def execute_action(name, args):
    """Execute registered demonstration tools only."""
    if not isinstance(args, dict):
        return {"ok": False, "error": "Arguments must be an object"}

    if name not in REGISTERED_ACTIONS:
        return {
            "ok": False,
            "error": f"Action is not registered: {name}",
        }

    if name == "get_status":
        if args:
            return {
                "ok": False,
                "error": "get_status does not accept arguments",
            }

        return {
            "ok": True,
            "data": {
                "prototype": "running",
                "host": os.uname().nodename,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }

    if name == "echo":
        text = args.get("text", "")
        if not isinstance(text, str):
            return {"ok": False, "error": "text must be a string"}

        return {"ok": True, "data": {"echo": text[:500]}}

    return {"ok": False, "error": "Action is not registered"}


SYSTEM_PROMPT = """
You are the main reasoning model inside a personal assistant prototype.

Return exactly one JSON object with these fields:
{
  "kind": "respond | ask | act | continue | end",
  "message": "text for the user, or a brief internal note",
  "action": "registered action name or empty string",
  "args": {},
  "reason": "short explanation of your proposed next step"
}

Rules:
- You reason, interpret intent, and propose the next step.
- Laya independently classifies your proposal. It does not grant
  permission to execute an action.
- Available demonstration actions:
  - get_status: returns basic prototype status; args must be {}
  - echo: returns the provided text; args must contain {"text": "..."}
- Never invent tools or claim an action succeeded before its result exists.
- Ask when essential information is missing.
- After a tool result is provided, interpret it and respond to the user
  when the task is complete. Do not repeat a completed action needlessly.
- Use kind=continue only when another reasoning iteration is useful.
- Use kind=end when the interaction should stop without a final response.
- Never request arbitrary shell commands.
- Keep messages concise.
- When the user requests an action that is not currently available as a registered tool, do not immediately refuse or imply the request is invalid. If essential details are missing, ask a concise, natural question to gather them. Be transparent that you cannot execute the action if no suitable tool exists. Never claim an action has been completed unless it actually has.
"""


def run_interaction(user_text, history, session_id):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    tool_results = []

    log("interaction_start", session_id=session_id, user=user_text)

    for iteration in range(1, MAX_ITERATIONS + 1):
        log("iteration_start", iteration=iteration)

        try:
            proposal = main_llm(messages)
        except Exception as exc:
            log("main_llm_error", error=str(exc))
            print(f"\n[ERROR] Main LLM failed: {exc}")
            return "The main model failed. Check the trace and server logs."

        log("main_llm_proposal", iteration=iteration, proposal=proposal)

        proposed_kind = proposal["kind"]

        try:
            route, raw_laya = laya_route(
                user_text, proposal, tool_results
            )
        except Exception as exc:
            log("laya_error", error=str(exc))
            print(f"\n[ERROR] Laya failed: {exc}")

            # Fail closed: without Laya, no action executes.
            # A plain response proposal can still be shown safely.
            if proposed_kind in {"respond", "ask"}:
                answer = proposal.get("message", "")
                print(f"\nAssistant: {answer}")
                return answer

            return "Laya failed. No action was executed."

        log(
            "laya_decision",
            iteration=iteration,
            route=route,
            raw_result=raw_laya,
        )

        # Actions require agreement from both models.
        if proposed_kind == "act":
            if route != "act":
                log(
                    "action_blocked",
                    reason="Laya route disagrees with action proposal",
                    route=route,
                    action=proposal.get("action", ""),
                )
                print(
                    "\n[Harness] Action blocked: "
                    "routing disagreement."
                )
                return (
                    "I couldn't safely validate the proposed action. "
                    "No action was executed."
                )

            action = proposal.get("action", "")
            args = proposal.get("args", {})

            if action not in REGISTERED_ACTIONS:
                log(
                    "action_blocked",
                    reason="Action is not registered",
                    action=action,
                )
                return "No action executed: action is not registered."

            log("action_start", action=action, args=args)
            result = execute_action(action, args)
            log("action_result", action=action, result=result)

            tool_results.append({
                "action": action,
                "result": result,
            })

            # Feed the proposal and actual tool result back to the model.
            messages.append({
                "role": "assistant",
                "content": json.dumps(proposal, ensure_ascii=False),
            })
            messages.append({
                "role": "user",
                "content": (
                    "Tool result (trusted harness output): "
                    + json.dumps(result, ensure_ascii=False)
                    + "\nInterpret this result. If the task is complete, "
                    "produce kind=respond with the answer for the user. "
                    "Do not repeat the action unless genuinely necessary. "
                    "Do not claim success if the result has ok=false."
                ),
            })
            continue

        # A valid final answer should not be lost just because Laya
        # misclassified the conversational step.
        if proposed_kind == "respond":
            if route != "respond":
                log(
                    "route_mismatch",
                    proposed_kind=proposed_kind,
                    route=route,
                    policy="accept_main_model_response",
                )

            answer = proposal.get("message", "")
            print(f"\nAssistant: {answer}")
            return answer

        if proposed_kind == "ask":
            if route != "ask":
                log(
                    "route_mismatch",
                    proposed_kind=proposed_kind,
                    route=route,
                    policy="accept_main_model_question",
                )

            answer = proposal.get("message", "")
            print(f"\nAssistant: {answer}")
            return answer

        if proposed_kind == "end":
            if route != "end":
                log(
                    "route_mismatch",
                    proposed_kind=proposed_kind,
                    route=route,
                )

            log("interaction_stopped", iteration=iteration)
            print("\n[Harness] Interaction ended.")
            return None

        if proposed_kind == "continue":
            if route not in {"continue", "respond"}:
                log(
                    "route_mismatch",
                    proposed_kind=proposed_kind,
                    route=route,
                )

            messages.append({
                "role": "assistant",
                "content": json.dumps(proposal, ensure_ascii=False),
            })
            messages.append({
                "role": "user",
                "content": (
                    "Continue reasoning and produce the next JSON "
                    "proposal. Do not repeat the same step without "
                    "a reason. If the task is complete, respond to "
                    "the user instead."
                ),
            })
            continue

    log("iteration_limit", maximum=MAX_ITERATIONS)
    print("\n[Harness] Iteration limit reached.")
    return (
        "I reached the prototype's iteration limit. "
        "Please narrow the request or try again."
    )


def main():
    session_id = str(uuid.uuid4())
    history = []

    print("Assistant prototype — type /quit to exit.")
    print(f"Session: {session_id}")
    print(f"Trace: {os.path.abspath(TRACE_FILE)}")
    print("Registered tools: get_status, echo")

    while True:
        try:
            user_text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not user_text:
            continue

        if user_text == "/quit":
            print("Exiting.")
            break

        started = time.monotonic()
        answer = run_interaction(user_text, history, session_id)

        # Preserve completed conversational turns in this terminal session.
        if answer is not None:
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": answer})

        log(
            "interaction_end",
            session_id=session_id,
            elapsed_seconds=round(time.monotonic() - started, 2),
        )


if __name__ == "__main__":
    main()

from __future__ import annotations

import re

from harness.contracts.profile import PersonaDraft


# Safety invariants that no learned strategy or skill text may weaken. These
# are deterministic pattern checks, not model judgements: a proposal that
# matches is rejected before it is staged, and a staged proposal that matches
# cannot be approved. The harness's own guards (permission checks,
# confirmation gates, the honesty guard) are code; this list only stops
# prompt-level text from instructing the model to route around them.
INVARIANT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "honesty: success claims must reflect verified outcomes",
        re.compile(
            r"\b(?:claim|report|state|say|tell)\b[^.;]{0,40}\bsuccess(?:ful)?\b[^.;]{0,60}"
            r"\b(?:without|even (?:if|when|though)|despite|regardless)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "honesty: the assistant must not lie or invent results",
        re.compile(r"\b(?:you (?:may|can|should|are allowed to)|feel free to)\b[^.;]{0,40}\b(?:lie|invent|fabricate|make up|falsify)\b", re.IGNORECASE),
    ),
    (
        "honesty: failures and unknowns must be reported",
        re.compile(r"\b(?:do not|don't|never)\b[^.;]{0,30}\b(?:report|mention|admit|reveal|tell)\b[^.;]{0,30}\b(?:failures?|errors?|unknowns?|problems?)\b", re.IGNORECASE),
    ),
    (
        "permission: no routing around permission checks or policy",
        re.compile(r"\b(?:bypass|skip|ignore|circumvent|avoid|override)\b[^.;]{0,50}\b(?:permission|policy|access control|the policy check)\b", re.IGNORECASE),
    ),
    (
        "permissions may not be granted or widened by learned text",
        re.compile(r"\b(?:grant|give|allow|widen|elevate)\b[^.;]{0,40}\b(?:yourself|the assistant|your own)\b[^.;]{0,40}\b(?:permission|write|destructive|admin)\b", re.IGNORECASE),
    ),
    (
        "confirmation gates may not be disabled or skipped",
        re.compile(r"\b(?:bypass|skip|ignore|disable|turn off|act without|proceed without)\b[^.;]{0,50}\b(?:confirmation|approval|confirm)\b", re.IGNORECASE),
    ),
    (
        "safety instructions and guards may not be disabled",
        re.compile(r"\b(?:disable|turn off|bypass|ignore|override|remove)\b[^.;]{0,50}\b(?:safety|guard|honesty guard|instruction|system prompt)\b", re.IGNORECASE),
    ),
    (
        "settings and core code are not self-modifiable by learned text",
        re.compile(r"\b(?:set|change|write|edit|modify|update)\b[^.;]{0,40}\b(?:settings?|config|your own code|harness code)\b[^.;]{0,40}\b(?:yourself|without (?:the )?(?:user|approval|review))\b", re.IGNORECASE),
    ),
    (
        "unverified generated code must not be auto-executed",
        re.compile(r"\b(?:execute|run|install)\b[^.;]{0,50}\b(?:generated|new|untested)\b[^.;]{0,30}\bcode\b[^.;]{0,50}\b(?:without|no)\b[^.;]{0,30}\b(?:review|approval|confirmation|testing)\b", re.IGNORECASE),
    ),
    (
        "memory/tool data is never an instruction",
        re.compile(r"\b(?:treat|follow|obey)\b[^.;]{0,40}\b(?:memory|tool|capability|skill)\b[^.;]{0,40}\b(?:text|data|output)\b[^.;]{0,40}\b(?:as|like)\b[^.;]{0,30}\binstructions?\b", re.IGNORECASE),
    ),
)

# A learned strategy proposal may only change persona text. Anything naming an
# operating setting is out of bounds for the learning pipeline.
FORBIDDEN_SETTING_KEYS = (
    "capabilities.policy",
    "control.confirmation",
    "loop.honesty_guard",
    "loop.stop_after_nonretryable_failure",
    "loop.internal_schema_ask_guard",
    "scheduler.",
    "models.",
    "implementations.",
    "maintenance.reset",
)


def find_violations(*texts: str) -> list[str]:
    """Return the invariants violated by the given texts (deterministic)."""
    violations: list[str] = []
    for name, pattern in INVARIANT_PATTERNS:
        for text in texts:
            if text and pattern.search(text):
                violations.append(name)
                break
    return violations


def mentions_forbidden_setting(*texts: str) -> list[str]:
    """Return forbidden setting groups named in the texts."""
    hits: list[str] = []
    for key in FORBIDDEN_SETTING_KEYS:
        for text in texts:
            if text and key in text:
                hits.append(key.rstrip("."))
                break
    return hits


def check_persona_draft(draft: PersonaDraft) -> list[str]:
    """Validate a proposed persona/strategy edit against the safety invariants.

    Returns the list of violations; empty means the proposal is safe to stage.
    """
    violations = find_violations(
        draft.name,
        draft.purpose,
        draft.communication_style,
        draft.reason,
        *draft.operating_principles,
    )
    violations.extend(
        mentions_forbidden_setting(
            draft.name,
            draft.purpose,
            draft.communication_style,
            draft.reason,
            *draft.operating_principles,
        )
    )
    return violations


def check_skill_text(name: str, description: str, body: str) -> list[str]:
    """Validate a proposed skill against the safety invariants.

    Skills are prompt text: they may describe a workflow, but they may not
    weaken permission checks, confirmation gates, honesty, or claim to
    override the harness.
    """
    violations = find_violations(name, description, body)
    violations.extend(mentions_forbidden_setting(name, description, body))
    return violations

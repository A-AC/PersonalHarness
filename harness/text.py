from __future__ import annotations

import math
import re

TOKEN_RE = re.compile(r"[a-z0-9]+")
THINK_BLOCK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
UNRESOLVED_PLACEHOLDER_RE = re.compile(r"<[^<>\n]{1,80}>")

# A compact, deliberately conservative test for a first-person success claim.
# It is shared by the loop's honesty guard and the eval checks so the harness
# and the evidence agree on what "claiming success" means.
_DONE_VERBS = (
    "sent|booked|deleted|removed|executed|ran|reserved|noted|remembered|saved|"
    "stored|completed|finished|done|written|created|added|updated|changed|set|"
    "checked|verified|confirmed|posted|uploaded|downloaded|moved|copied|renamed|"
    "enabled|disabled|started|stopped|restarted"
)
SUCCESS_CLAIM_RE = re.compile(
    rf"\b(i(?:'ve| have)?\s+(?:{_DONE_VERBS})"
    rf"|has been (?:{_DONE_VERBS})"
    rf"|successfully (?:{_DONE_VERBS})"
    r"|(?:saved|stored|noted|remembered|written|created|updated|changed|set|checked|verified|confirmed) (?:it|that|this)"
    r"|(?:it|that|this|the task) (?:is|has been) (?:saved|stored|noted|remembered|done|complete|completed|finished))\b",
    re.IGNORECASE,
)

# A one-word answer such as "Done." is still a success claim after a failure.
BARE_SUCCESS_RE = re.compile(
    r"^\s*(?:done|saved|stored|noted|remembered|completed|finished|success(?:ful|fully)?|all done|all set)\s*[.!]?\s*$",
    re.IGNORECASE,
)

UNKNOWN_OUTCOME_TERMS = (
    "unknown",
    "uncertain",
    "not sure",
    "can't confirm",
    "cannot confirm",
    "couldn't confirm",
    "could not confirm",
    "unconfirmed",
    "not confirmed",
    "don't know",
    "do not know",
    "may have",
    "might have",
)


def claims_success(text: str) -> bool:
    value = text or ""
    return bool(SUCCESS_CLAIM_RE.search(value) or BARE_SUCCESS_RE.search(value))


def states_unknown_outcome(text: str) -> bool:
    lowered = (text or "").lower()
    return any(term in lowered for term in UNKNOWN_OUTCOME_TERMS)


def strip_reasoning(text: str) -> str:
    """Return only the user-visible text from a model response.

    llama-server returns Qwen reasoning in ``reasoning_content``, but some
    templates inline it as ``<think>...</think>``. Both must be excluded from
    visible-output accounting.
    """
    return THINK_BLOCK_RE.sub("", text or "").strip()


def matches_keywords(text: str, raw_keywords: str) -> bool:
    """Return true when any comma-separated keyword occurs in ``text``."""
    lowered = (text or "").lower()
    keywords = [item.strip().lower() for item in (raw_keywords or "").split(",") if item.strip()]
    return any(keyword in lowered for keyword in keywords)


def word_tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


def normalize_name(value: str) -> str:
    """Normalize a domain/project name for matching.

    Lowercases and keeps alphanumeric tokens only, so punctuation and repeated
    whitespace do not make two names look different.
    """
    return " ".join(TOKEN_RE.findall((value or "").lower()))


def name_overlap(a: str, b: str) -> float:
    """Containment-style overlap for short names.

    Jaccard overlap is too harsh for names ("home lab" vs "home lab notes").
    Divide the shared tokens by the smaller token set so a name contained in a
    longer one scores high; identical normalized names score 1.0.
    """
    if normalize_name(a) == normalize_name(b):
        return 1.0 if normalize_name(a) else 0.0
    left, right = word_tokens(a), word_tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def match_by_name(items, name: str):
    """Resolve an exact name, then a normalized match.

    Returns ``(match, candidates)``. A match is returned only when it is
    unambiguous. Otherwise ``candidates`` lists the names that collided so the
    caller can report them without creating or guessing structure.
    """
    wanted = (name or "").strip()
    if not wanted:
        return None, []
    exact = [item for item in items if item.name.strip().lower() == wanted.lower()]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, [item.name for item in exact]
    normalized = normalize_name(wanted)
    if not normalized:
        return None, []
    matches = [item for item in items if normalize_name(item.name) == normalized]
    if len(matches) == 1:
        return matches[0], []
    return None, [item.name for item in matches]


def overlap(a: str, b: str) -> float:
    left, right = word_tokens(a), word_tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def estimate_tokens(text: str, *, chars_per_token: int = 4) -> int:
    if not text:
        return 0
    size = max(1, chars_per_token)
    return max(1, math.ceil(len(text) / size))

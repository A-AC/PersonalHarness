from __future__ import annotations

import math
import re

TOKEN_RE = re.compile(r"[a-z0-9]+")


def word_tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


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

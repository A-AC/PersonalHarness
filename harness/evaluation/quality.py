"""Rubric scoring for open-ended replies.

Quality is deliberately separate from pass/fail assertions. A rubric answers
"how well did the reply satisfy the requested behaviour?" while assertions
answer "did the harness persist the required state?". A failed criterion lowers
the score but never changes the scenario's pass/fail status, so behavior and
quality stay on separate axes in reports and comparisons.

Criteria are local and deterministic: substring, regex, and length checks. They
are a reproducible rubric, not an LLM judge. Use a judge when subjective
judgment is the thing being measured; this module keeps the default suite
offline and repeatable.
"""

from __future__ import annotations

import re
from typing import Any

from .assertions import Observation


def evaluate_quality(observation: Observation) -> dict[str, Any]:
    spec = observation.step.expect.get("quality")
    if not isinstance(spec, dict) or not spec:
        return {}
    response = str(getattr(observation.result, "message", "") or "")
    raw = spec.get("rubric") or []
    criteria: list[dict[str, Any]] = []
    earned = 0.0
    total = 0.0
    for index, item in enumerate(raw):
        criterion = item if isinstance(item, dict) else {"any": [str(item)]}
        weight = _number(criterion.get("weight"), 1.0)
        total += weight
        passed, observed = _criterion(response, criterion)
        if passed:
            earned += weight
        criteria.append({
            "id": str(criterion.get("id") or f"criterion{index}"),
            "description": str(criterion.get("description") or ""),
            "weight": weight,
            "status": "passed" if passed else "failed",
            "observed": observed,
        })
    score = round(earned / total, 4) if total else None
    minimum = spec.get("min_score")
    out: dict[str, Any] = {
        "score": score,
        "criteria": criteria,
        "min_score": _number(minimum, None) if minimum is not None else None,
    }
    if out["min_score"] is not None and score is not None:
        out["passed"] = score >= out["min_score"]
    return out


def _criterion(response: str, criterion: dict[str, Any]) -> tuple[bool, str]:
    text = response.lower()
    passed = True
    if "any" in criterion:
        needles = _list(criterion["any"])
        passed = passed and any(str(item).lower() in text for item in needles)
    if "all" in criterion:
        passed = passed and all(
            str(item).lower() in text for item in _list(criterion["all"])
        )
    if "none" in criterion:
        passed = passed and not any(
            str(item).lower() in text for item in _list(criterion["none"])
        )
    if "regex" in criterion:
        pattern = str(criterion["regex"])
        if "min_matches" in criterion or "max_matches" in criterion:
            count = len(re.findall(pattern, response, re.IGNORECASE))
            if "min_matches" in criterion:
                passed = passed and count >= int(criterion["min_matches"])
            if "max_matches" in criterion:
                passed = passed and count <= int(criterion["max_matches"])
        else:
            passed = passed and re.search(
                pattern, response, re.IGNORECASE
            ) is not None
    words = len(re.findall(r"\S+", response))
    if "min_words" in criterion:
        passed = passed and words >= int(criterion["min_words"])
    if "max_words" in criterion:
        passed = passed and words <= int(criterion["max_words"])
    return passed, response[:500]


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _number(value: Any, default: float | None) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default if default is not None else 0.0

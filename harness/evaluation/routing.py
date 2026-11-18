"""Pure routing-evaluation metrics shared by the sweep script and its tests.

These helpers deliberately know nothing about the harness, YAML, or the
command line: they take labelled names in and return deterministic counts.
That keeps a baseline report reproducible and lets tests pin the arithmetic
without building an App.
"""

from __future__ import annotations

from typing import Any, Iterable


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def routing_metrics(
    expected: Iterable[str], predicted: Iterable[str]
) -> dict[str, Any]:
    """Accuracy, fallback rate, false-positive rate, and a confusion table.

    ``expected`` and ``predicted`` are domain *names* in the same order.
    A prediction of ``General`` when a specific domain was expected is a miss
    (counted by the fallback rate), not a false positive: General is the
    documented safe fallback. Routing to a specific domain that differs from
    the expectation — including a specific domain when General was expected —
    is the dangerous false positive.
    """
    expected_list = [str(item) for item in expected]
    predicted_list = [str(item) for item in predicted]
    if len(expected_list) != len(predicted_list):
        raise ValueError("expected and predicted must have the same length")

    total = len(expected_list)
    correct = fallbacks = false_positives = misses = 0
    for want, got in zip(expected_list, predicted_list):
        want_key = want.strip().lower()
        got_key = got.strip().lower()
        if want_key == got_key:
            correct += 1
        if got_key == "general":
            fallbacks += 1
            if want_key != "general":
                misses += 1
        elif want_key != got_key:
            false_positives += 1

    return {
        "total": total,
        "correct": correct,
        "accuracy": _rate(correct, total),
        "general_fallback_count": fallbacks,
        "general_fallback_rate": _rate(fallbacks, total),
        "false_positive_count": false_positives,
        "false_positive_rate": _rate(false_positives, total),
        "misses": misses,
        "confusion": confusion_table(expected_list, predicted_list),
    }


def confusion_table(
    expected: Iterable[str], predicted: Iterable[str]
) -> dict[str, Any]:
    """Counts by expected (row) and predicted (column) domain names."""
    expected_list = [str(item) for item in expected]
    predicted_list = [str(item) for item in predicted]
    if len(expected_list) != len(predicted_list):
        raise ValueError("expected and predicted must have the same length")

    # Fusion is case-insensitive, like routing_metrics. Keep the first
    # human-readable spelling seen for each key.
    canonical: dict[str, str] = {}

    def label(value: str) -> str:
        key = value.strip().casefold()
        if key not in canonical:
            canonical[key] = value.strip()
        return canonical[key]

    expected_labels = [label(item) for item in expected_list]
    predicted_labels = [label(item) for item in predicted_list]
    labels = sorted(set(expected_labels) | set(predicted_labels))
    rows = {label_name: {column: 0 for column in labels}
            for label_name in labels}
    for want, got in zip(expected_labels, predicted_labels):
        rows[want][got] += 1
    return {"labels": labels, "rows": rows}


def best_grid_point(grid: list[dict[str, Any]]) -> dict[str, Any]:
    """Best accuracy; ties break to the higher threshold then lower margin."""
    if not grid:
        raise ValueError("grid is empty")
    return max(
        grid,
        key=lambda point: (
            float(point.get("accuracy") or 0.0),
            float(point.get("threshold") or 0.0),
            -float(point.get("margin") or 0.0),
        ),
    )

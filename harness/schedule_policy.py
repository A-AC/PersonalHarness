"""Explicit, bounded capabilities for unattended schedule occurrences.

A schedule approval may authorize fixed web-search templates and, optionally,
one tainted task-notes write per occurrence. It never grants a general taint or
approval exemption. Only date placeholders are expanded, so retrieved content
cannot choose a new destination or inject prompt-derived data into a query.
"""

from __future__ import annotations

import datetime as dt
import string
from typing import Any


TIME_RANGES = frozenset({"day", "week", "month", "year"})
PLACEHOLDERS = frozenset({
    "current_date", "current_year", "current_month",
})
MAX_QUERIES = 4
WEEKDAYS = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})


def validate_schedule_request(arguments: dict[str, Any]) -> None:
    """Reject underspecified schedules before an approval card is created."""
    kind = str(arguments.get("kind") or "")
    if kind not in ("once", "interval", "daily", "weekly"):
        raise ValueError(f"unknown schedule kind: {kind}")
    if kind == "once" and not str(arguments.get("at_ts") or "").strip():
        raise ValueError("once schedule needs at_ts")
    if kind == "interval":
        interval = arguments.get("interval_s")
        if isinstance(interval, bool) or not isinstance(interval, (int, float)):
            raise ValueError("interval schedule needs a numeric interval_s")
        if interval <= 0:
            raise ValueError("interval schedule needs interval_s > 0")
    if kind in ("daily", "weekly"):
        parts = str(arguments.get("time_of_day") or "").split(":")
        if (
            len(parts) != 2
            or not all(part.isdigit() for part in parts)
            or not 0 <= int(parts[0]) <= 23
            or not 0 <= int(parts[1]) <= 59
        ):
            raise ValueError(f"{kind} schedule needs time_of_day in HH:MM format")
    if kind == "weekly" and (
        str(arguments.get("weekday") or "").strip().lower()[:3] not in WEEKDAYS
    ):
        raise ValueError("weekly schedule needs a valid weekday")
    if "unattended" in arguments:
        validate_unattended(arguments.get("unattended"))


def validate_unattended(value: Any) -> dict[str, Any] | None:
    """Return a normalized policy or raise ValueError for unsafe capabilities."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("unattended must be an object")
    unknown = set(value) - {"web_queries", "allow_task_notes"}
    if unknown:
        raise ValueError(
            "unknown unattended fields: " + ", ".join(sorted(unknown))
        )
    allow_notes = value.get("allow_task_notes", False)
    if not isinstance(allow_notes, bool):
        raise ValueError("unattended.allow_task_notes must be boolean")
    raw_queries = value.get("web_queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError("unattended.web_queries needs at least one query")
    if len(raw_queries) > MAX_QUERIES:
        raise ValueError(
            f"unattended.web_queries is capped at {MAX_QUERIES} queries"
        )
    queries: list[dict[str, Any]] = []
    for raw in raw_queries:
        if not isinstance(raw, dict):
            raise ValueError("each unattended web query must be an object")
        unknown = set(raw) - {"query", "categories", "max_results", "time_range"}
        if unknown:
            raise ValueError(
                "unknown unattended query fields: " + ", ".join(sorted(unknown))
            )
        query = str(raw.get("query") or "").strip()
        if not query or len(query) > 300:
            raise ValueError("unattended query needs 1-300 characters")
        try:
            parsed = list(string.Formatter().parse(query))
        except ValueError as exc:
            raise ValueError(f"invalid unattended query placeholder: {exc}") from exc
        for _literal, field, format_spec, conversion in parsed:
            if field is None:
                continue
            if field not in PLACEHOLDERS or format_spec or conversion:
                raise ValueError(
                    "unattended queries allow only simple date placeholders: "
                    + ", ".join(sorted(PLACEHOLDERS))
                )
        categories = str(raw.get("categories") or "").strip()
        if len(categories) > 80:
            raise ValueError("unattended categories are capped at 80 characters")
        max_results = raw.get("max_results", 5)
        if (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or not 1 <= max_results <= 10
        ):
            raise ValueError("unattended max_results must be an integer from 1-10")
        time_range = str(raw.get("time_range") or "").strip()
        if time_range and time_range not in TIME_RANGES:
            raise ValueError(
                "unattended time_range must be day, week, month, or year"
            )
        queries.append({
            "query": query,
            "categories": categories,
            "max_results": max_results,
            "time_range": time_range,
        })
    return {"web_queries": queries, "allow_task_notes": allow_notes}


def allowed_tools(policy: dict[str, Any] | None) -> set[str]:
    if not policy:
        return set()
    tools = {"web_search"}
    if policy.get("allow_task_notes"):
        tools.add("update_task_notes")
    return tools


def _values(now: dt.datetime) -> dict[str, str]:
    return {
        "current_date": now.strftime("%Y-%m-%d"),
        "current_year": now.strftime("%Y"),
        "current_month": now.strftime("%Y-%m"),
    }


def render_query(template: str, now: dt.datetime) -> str:
    return template.format_map(_values(now))


def authorizes_web_search(
    policy: dict[str, Any] | None,
    arguments: dict[str, Any],
    now: dt.datetime,
) -> bool:
    """Compare a call semantically with one explicitly approved template."""
    if not policy:
        return False
    query = str(arguments.get("query") or "")
    categories = str(arguments.get("categories") or "")
    time_range = str(arguments.get("time_range") or "")
    max_results = arguments.get("max_results", 5)
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        return False
    for template in policy.get("web_queries") or []:
        if (
            query == render_query(str(template.get("query") or ""), now)
            and categories == str(template.get("categories") or "")
            and time_range == str(template.get("time_range") or "")
            and max_results == int(template.get("max_results") or 5)
        ):
            return True
    return False

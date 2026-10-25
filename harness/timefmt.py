"""Compact local-time rendering shared by prompts, history, and the UI.

Storage keeps UTC ISO timestamps. The model sees the configured local timezone
so "yesterday", "this morning", and "3 days ago" line up with the user's day.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from harness.contracts.settings import SettingsService
from harness.storage.atomic import now_ts


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timezone_name(settings: SettingsService) -> str:
    return str(settings.get("context.timezone") or "UTC")


def resolve_timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # A missing tz database or a typo must not break a turn.
        return ZoneInfo("UTC")


def to_local(value: str | datetime | None, name: str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = parse_ts(value)
    else:
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed is None:
        return None
    return parsed.astimezone(resolve_timezone(name))


def _plural(value: int, unit: str) -> str:
    return f"{value} {unit}" if value == 1 else f"{value} {unit}s"


def human_gap(seconds: float) -> str:
    """Return a compact relative phrase for a notable gap."""
    value = max(0, int(seconds))
    minutes = value // 60
    if minutes < 60:
        return _plural(max(1, minutes), "minute")
    hours = minutes // 60
    if hours < 48:
        return _plural(hours, "hour")
    days = hours // 24
    if days < 14:
        return _plural(days, "day")
    weeks = days // 7
    if weeks < 9:
        return _plural(weeks, "week")
    months = days // 30
    if months < 24:
        return _plural(max(1, months), "month")
    return _plural(max(1, days // 365), "year")


def gap_between(earlier: str | None, later: str | None) -> float:
    left = parse_ts(earlier)
    right = parse_ts(later)
    if left is None or right is None:
        return 0.0
    return max(0.0, (right - left).total_seconds())


def time_marker(value: str | None, *, timezone_name: str, now: str | None = None) -> str:
    """A short ``MM-DD HH:MM`` marker, with a year only when it differs."""
    local = to_local(value, timezone_name)
    if local is None:
        return "unknown time"
    reference = to_local(now, timezone_name) if now else None
    if reference is None or local.year == reference.year:
        return local.strftime("%m-%d %H:%M")
    return local.strftime("%Y-%m-%d %H:%M")


def current_time_block(settings: SettingsService) -> str:
    name = timezone_name(settings)
    local = to_local(now_ts(), name)
    utc = parse_ts(now_ts())
    if local is None or utc is None:
        return "Current time: unknown"
    return (
        f"Current time: {local:%a %Y-%m-%d %H:%M} {name} "
        f"(UTC {utc:%H:%M})"
    )


def summary_window_label(value: str | None, *, timezone_name: str) -> str:
    local = to_local(value, timezone_name)
    if local is None:
        return "earlier"
    return local.strftime("%Y-%m-%d %H:%M")


def iso_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="microseconds").replace("+00:00", "Z")

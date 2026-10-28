"""Small shared helpers: ids, timestamps, JSON, text."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
from typing import Any

UTC = dt.timezone.utc


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def _system_zone_name() -> str | None:
    """Best-effort IANA name for the host's local timezone."""
    env = os.environ.get("TZ", "").strip()
    if env:
        return env
    # Linux/macOS expose the chosen zone as a symlink into the tz database.
    try:
        target = os.path.realpath("/etc/localtime")
        marker = "zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1]
    except OSError:
        pass
    tz = dt.datetime.now().astimezone().tzinfo
    key = getattr(tz, "key", None)
    return str(key) if key else None


def system_tz() -> dt.tzinfo:
    """The host's current local timezone.

    Prefers a real IANA zone when the OS exposes one (so future daylight
    transitions are handled); otherwise falls back to the fixed offset the
    system clock reports right now.
    """
    import zoneinfo

    name = _system_zone_name()
    if name:
        try:
            return zoneinfo.ZoneInfo(name)
        except Exception:
            pass
    return dt.datetime.now().astimezone().tzinfo or UTC


def system_tz_name() -> str:
    """Human-readable name for the host timezone."""
    name = _system_zone_name()
    if name:
        return name
    zone = system_tz()
    return str(zone.tzname(None) or zone)


def iso(ts: dt.datetime | None = None) -> str:
    """UTC ISO-8601 with Z suffix; second precision is enough everywhere."""
    if ts is None:
        ts = now_utc()
    return ts.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> dt.datetime:
    """Parse a stored UTC timestamp. Returns epoch for unparsable input."""
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, AttributeError):
        return dt.datetime(1970, 1, 1, tzinfo=UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def loads(text: str, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def token_len(text: str) -> int:
    """Rough token estimate used only for budgets: ~4 characters per token."""
    return max(1, (len(text) + 3) // 4)


_WORD_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_\-']{1,}")

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "when", "at",
    "by", "for", "with", "about", "into", "to", "from", "in", "on", "of", "is",
    "are", "was", "were", "be", "been", "am", "i", "you", "he", "she", "it",
    "we", "they", "me", "him", "her", "us", "them", "my", "your", "his",
    "its", "our", "their", "this", "that", "these", "those", "what", "which",
    "who", "how", "why", "where", "do", "does", "did", "can", "could",
    "should", "would", "will", "shall", "may", "might", "must", "have", "has",
    "had", "not", "no", "yes", "so", "as", "than", "too", "very", "just",
    "please", "tell", "ask", "let", "get", "make", "want", "need", "like",
}


def keywords(text: str) -> list[str]:
    """Lowercased content words, stopword-free, de-duplicated in order."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _WORD_RE.findall(text or ""):
        word = match.lower()
        if word in STOPWORDS or word in seen:
            continue
        seen.add(word)
        out.append(word)
    return out


def clip(text: str, limit: int) -> str:
    """Trim text to a budget, cutting on a boundary and marking the cut."""
    if len(text) <= limit:
        return text
    cut = text[: max(1, limit - 15)]
    if " " in cut[1:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip() + " …[trimmed]"

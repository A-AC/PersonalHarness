from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class HistoryNote:
    """One inclusion/exclusion decision made while building history."""

    item: str
    reason: str


@dataclass
class HistorySegment:
    """A stored conversation segment and its rolling summary.

    Segments are derived from a gap between interactions (or a single rolling
    segment for the summary strategy). ``covered_ids`` is always a prefix of
    ``interaction_ids``: the summary covers those interactions, and anything
    after it is still carried verbatim.
    """

    id: str
    session_id: str
    started_at: str
    ended_at: str
    interaction_ids: list[str]
    summary: str | None = None
    covered_ids: list[str] = field(default_factory=list)
    summary_ts: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    status: str = "pending"
    error: str | None = None


@dataclass
class HistoryView:
    """Rendered history text plus the decisions that produced it."""

    text: str
    tokens: int
    mode: str
    segment_id: str | None = None
    summary_used: bool = False
    verbatim_count: int = 0
    older_count: int = 0
    included: list[HistoryNote] = field(default_factory=list)
    excluded: list[HistoryNote] = field(default_factory=list)
    stats: dict[str, object] = field(default_factory=dict)


class HistoryService(Protocol):
    def build(self) -> HistoryView: ...

    def after_turn(self, interaction_id: str) -> None: ...

    def summarize_pending(self, *, force: bool = False) -> list[HistorySegment]: ...

    def list_segments(self) -> list[HistorySegment]: ...

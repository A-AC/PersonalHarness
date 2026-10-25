from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from harness.contracts.events import Event, EventLog
from harness.contracts.history import HistoryNote, HistorySegment, HistoryView
from harness.contracts.model import Message, ModelAdapter, ModelUnavailable
from harness.contracts.session import Interaction, SessionManager
from harness.contracts.settings import SettingsService
from harness.history.store import FileHistoryStore
from harness.prompts import SUMMARY_INSTRUCTIONS
from harness.storage.atomic import new_id, now_ts
from harness.text import estimate_tokens, strip_reasoning
from harness.timefmt import gap_between, human_gap, summary_window_label, time_marker, timezone_name


class FileHistoryService:
    """Budgeted history with optional rolling summaries and gap segments.

    The class only reads session interactions and writes its own summary files.
    Context construction depends on the ``HistoryService`` contract, so another
    implementation can replace it without touching the loop or the builder.
    """

    def __init__(
        self,
        directory: Path,
        settings: SettingsService,
        sessions: SessionManager,
        model: ModelAdapter,
        events: EventLog,
    ):
        self._settings = settings
        self._sessions = sessions
        self._model = model
        self._events = events
        self._store = FileHistoryStore(directory / "segments", settings)
        self._lock = threading.Lock()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Wait for an off-path summary write (used by reset)."""
        with self._lock:
            yield

    # ------------------------------------------------------------------ build
    def build(self) -> HistoryView:
        interactions = list(self._sessions.list_interactions(limit=None))
        mode = str(self._settings.get("context.history.strategy") or "segment")
        budget = int(self._settings.get("context.budgets.history"))
        if not interactions or budget <= 0:
            excluded = (
                [HistoryNote(item="history:overflow", reason="history token budget is zero")]
                if interactions
                else []
            )
            return HistoryView(
                text="",
                tokens=0,
                mode=mode,
                stats={"budget": budget, "interactions": len(interactions)},
                excluded=excluded,
            )

        tz = timezone_name(self._settings)
        now = now_ts()
        chars = int(self._settings.get("context.chars_per_token"))
        max_chars = int(self._settings.get("context.history.max_message_chars"))
        gap_hours = float(self._settings.get("context.time.gap_hours"))
        verbatim_turns = int(self._settings.get("context.history.verbatim_turns"))

        segments = self._segments(interactions, mode)
        current = segments[-1]
        by_id = {item.id: item for item in interactions}
        current_items = [by_id[item] for item in current.interaction_ids if item in by_id]

        included: list[HistoryNote] = []
        excluded: list[HistoryNote] = []
        summary_parts: list[tuple[str, str, HistorySegment]] = []
        fallback_older: list[Interaction] = []

        if mode == "full":
            recent = interactions
            older: list[Interaction] = []
        else:
            split = max(0, len(current_items) - max(0, verbatim_turns))
            older = current_items[:split]
            recent = current_items[split:]
            if mode == "segment" and len(segments) > 1:
                previous = segments[-2]
                previous_items = [by_id[item] for item in previous.interaction_ids if item in by_id]
                stored, covered_count = self._summary_prefix(previous, previous.interaction_ids)
                if stored is not None:
                    summary_parts.append(
                        (
                            f"Summary of the conversation before "
                            f"{summary_window_label(previous.ended_at, timezone_name=tz)}:",
                            stored.summary or "",
                            stored,
                        )
                    )
                    # A summary written before the last turn may cover only a
                    # prefix of the segment. Keep the rest verbatim rather than
                    # discarding a useful summary.
                    fallback_older.extend(previous_items[covered_count:])
                else:
                    fallback_older.extend(previous_items)
                    excluded.append(
                        HistoryNote(
                            item=f"history:summary:{previous.id}",
                            reason="summary missing; falling back to verbatim truncation",
                        )
                    )
            older_ids = [item.id for item in older]
            stored, covered_count = self._summary_prefix(current, older_ids)
            if stored is not None:
                summary_parts.append(("Earlier in this conversation:", stored.summary or "", stored))
                fallback_older.extend(older[covered_count:])
            elif older:
                fallback_older.extend(older)
                excluded.append(
                    HistoryNote(
                        item=f"history:summary:{current.id}",
                        reason="summary missing; falling back to verbatim truncation",
                    )
                )

        summary_cap = int(self._settings.get("context.history.summary_tokens")) if summary_parts else 0
        summary_cap = max(0, min(summary_cap, budget))
        verbatim_cap = max(0, budget - summary_cap)
        prefix_ts = None
        if recent and mode != "full":
            before = interactions[: interactions.index(recent[0])]
            if before:
                prefix_ts = before[-1].assistant_ts or before[-1].ts
        lines, included_recent, dropped_recent = self._select_recent(
            recent,
            verbatim_cap,
            chars=chars,
            max_chars=max_chars,
            gap_hours=gap_hours,
            tz=tz,
            now=now,
            reason="verbatim: latest turns" if mode != "full" else "verbatim: full history",
            prefix_ts=prefix_ts,
        )
        for item in included_recent:
            included.append(HistoryNote(item=f"history:{item}", reason="verbatim"))
        for item, reason in dropped_recent:
            excluded.append(HistoryNote(item=f"history:{item}", reason=reason))

        used_summary_ids: set[str] = set()
        used_summary_covered: set[str] = set()
        summary_lines: list[str] = []
        remaining = max(0, budget - estimate_tokens("\n".join(lines), chars_per_token=chars))
        for label, text, segment in summary_parts:
            if remaining <= 0:
                excluded.append(HistoryNote(item=f"history:summary:{segment.id}", reason="over token budget"))
                continue
            allowed = min(remaining, summary_cap)
            allowed = max(0, allowed)
            if allowed <= 0:
                excluded.append(HistoryNote(item=f"history:summary:{segment.id}", reason="over token budget"))
                continue
            clipped = self._fit_text(text.strip(), allowed, chars)
            if not clipped:
                excluded.append(HistoryNote(item=f"history:summary:{segment.id}", reason="over token budget"))
                continue
            summary_lines.append(f"{label} {clipped}")
            used_summary_ids.add(segment.id)
            used_summary_covered.update(segment.covered_ids)
            included.append(
                HistoryNote(
                    item=f"history:summary:{segment.id}",
                    reason=f"summary of {len(segment.covered_ids)} earlier turns",
                )
            )
            remaining = max(0, remaining - estimate_tokens(summary_lines[-1], chars_per_token=chars))

        fallback_prefix = None
        if fallback_older:
            before = interactions[: interactions.index(fallback_older[0])]
            if before:
                fallback_prefix = before[-1].assistant_ts or before[-1].ts
        fallback_lines, included_fallback, dropped_fallback = self._select_recent(
            fallback_older,
            remaining,
            chars=chars,
            max_chars=max_chars,
            gap_hours=gap_hours,
            tz=tz,
            now=now,
            reason="verbatim fallback: no summary",
            prefix_ts=fallback_prefix,
        )
        for item in included_fallback:
            included.append(HistoryNote(item=f"history:{item}", reason="verbatim fallback"))
        for item, reason in dropped_fallback:
            excluded.append(HistoryNote(item=f"history:{item}", reason=reason))

        text_parts = summary_lines + fallback_lines + lines
        text = "\n".join(part for part in text_parts if part)
        tokens = estimate_tokens(text, chars_per_token=chars)
        if tokens > budget:
            text = self._fit_text(text, budget, chars)
            tokens = estimate_tokens(text, chars_per_token=chars)

        # Report everything that did not make it into the prompt. Everything
        # stays in storage; only the model view is trimmed.
        seen = set(included_recent) | set(included_fallback)
        for item in interactions:
            if item.id in seen:
                continue
            if any(note.item == f"history:{item.id}" for note in excluded):
                continue
            excluded.append(
                HistoryNote(
                    item=f"history:{item.id}",
                    reason=(
                        "represented by a stored summary"
                        if item.id in used_summary_covered
                        else "not included in the prompt history budget"
                    ),
                )
            )

        older_count = max(0, len(current_items) - len(recent))
        verbatim_count = len(included_recent) + len(included_fallback)
        return HistoryView(
            text=text,
            tokens=tokens,
            mode=mode,
            segment_id=current.id,
            summary_used=bool(used_summary_ids),
            verbatim_count=verbatim_count,
            older_count=older_count,
            included=included,
            excluded=excluded,
            stats={
                "mode": mode,
                "budget": budget,
                "verbatim_turns": verbatim_turns,
                "segments": len(segments),
                "segment": current.id,
                "summary_used": bool(used_summary_ids),
                "summary_segments": sorted(used_summary_ids),
                "verbatim": verbatim_count,
                "older_turns": older_count,
                "interactions": len(interactions),
            },
        )

    # -------------------------------------------------------------- lifecycle
    def after_turn(self, interaction_id: str) -> None:
        if not bool(self._settings.get("context.history.summaries")):
            return
        if not bool(self._settings.get("context.history.background_summary")):
            return
        if str(self._settings.get("context.history.strategy")) == "full":
            return
        if self._pending_turns() < int(self._settings.get("context.history.summary_batch_turns")):
            return
        if not self._lock.acquire(blocking=False):
            return

        def run() -> None:
            try:
                self.summarize_pending()
            finally:
                self._lock.release()

        threading.Thread(target=run, name="harness-history-summary", daemon=True).start()

    def summarize_pending(self, *, force: bool = False) -> list[HistorySegment]:
        if str(self._settings.get("context.history.strategy")) == "full":
            return []
        if not bool(self._settings.get("context.history.summaries")):
            return []
        interactions = list(self._sessions.list_interactions(limit=None))
        if not interactions:
            return []
        mode = str(self._settings.get("context.history.strategy") or "segment")
        segments = self._segments(interactions, mode)
        by_id = {item.id: item for item in interactions}
        verbatim_turns = int(self._settings.get("context.history.verbatim_turns"))
        batch = int(self._settings.get("context.history.summary_batch_turns"))
        updated: list[HistorySegment] = []
        for segment in segments:
            is_current = segment.id == segments[-1].id
            target_count = len(segment.interaction_ids)
            if is_current:
                target_count = max(0, target_count - max(0, verbatim_turns))
            target_ids = segment.interaction_ids[:target_count]
            if not target_ids:
                continue
            stored = self._store.get(segment.id)
            covered = (
                list(stored.covered_ids)
                if stored is not None and stored.summary and stored.covered_ids
                else []
            )
            if covered != target_ids[: len(covered)]:
                covered = []
            pending = target_ids[len(covered) :]
            if not pending:
                if stored is not None and stored.status != "ok":
                    stored.status = "ok"
                    stored.error = None
                    self._store.put(stored)
                    updated.append(stored)
                continue
            if not force and is_current and len(pending) < batch:
                continue
            segment = stored or segment
            segment.summary = segment.summary if covered else None
            segment.covered_ids = covered
            changed = self._summarize_segment(segment, target_ids, by_id)
            if changed is not None:
                updated.append(changed)
        return updated

    def list_segments(self) -> list[HistorySegment]:
        return self._store.list()

    # ----------------------------------------------------------------- helpers
    def _summarize_segment(
        self,
        segment: HistorySegment,
        target_ids: list[str],
        by_id: dict[str, Interaction],
    ) -> HistorySegment | None:
        chars = int(self._settings.get("context.chars_per_token"))
        max_summary = int(self._settings.get("context.history.summary_tokens"))
        input_tokens = int(self._settings.get("context.history.summary_input_tokens"))
        covered = list(segment.covered_ids)
        changed = False
        while len(covered) < len(target_ids):
            next_ids = target_ids[len(covered) :]
            chunk: list[str] = []
            used = 0
            for ident in next_ids:
                line = self._summary_line(by_id[ident])
                cost = estimate_tokens(line, chars_per_token=chars)
                if chunk and used + cost > input_tokens:
                    break
                chunk.append(ident)
                used += cost
            if not chunk:
                chunk = [next_ids[0]]
            messages = self._summary_messages(segment, chunk, by_id)
            started = now_ts()
            try:
                result = self._model.complete("background", messages, schema=None)
            except ModelUnavailable as exc:
                segment.status = "failed"
                segment.error = str(exc)
                self._store.put(segment)
                self._events.append(
                    Event(
                        id=new_id("evt"),
                        ts=started,
                        actor="harness",
                        operation="history.summary",
                        target=segment.id,
                        result="unavailable",
                        data={"error": str(exc), "covered": len(covered)},
                    )
                )
                return segment if changed else None
            text = strip_reasoning(result.content).strip()
            if not text:
                segment.status = "failed"
                segment.error = "empty summary"
                self._store.put(segment)
                self._events.append(
                    Event(
                        id=new_id("evt"),
                        ts=started,
                        actor="harness",
                        operation="history.summary",
                        target=segment.id,
                        result="empty",
                        data={"covered": len(covered)},
                    )
                )
                return segment if changed else None
            segment.summary = self._fit_text(text, max_summary, chars)
            covered.extend(chunk)
            segment.covered_ids = list(covered)
            segment.summary_ts = now_ts()
            segment.model = str(self._settings.get("models.background.model_name"))
            segment.prompt_tokens = int(result.prompt_tokens or 0)
            segment.completion_tokens = int(result.completion_tokens or 0)
            segment.status = "ok"
            segment.error = None
            self._store.put(segment)
            changed = True
            self._events.append(
                Event(
                    id=new_id("evt"),
                    ts=started,
                    actor="harness",
                    operation="history.summary",
                    target=segment.id,
                    result="ok",
                    data={
                        "covered": len(covered),
                        "turns": len(chunk),
                        "prompt_tokens": segment.prompt_tokens,
                        "completion_tokens": segment.completion_tokens,
                    },
                )
            )
        return segment if changed else None

    def _summary_messages(
        self,
        segment: HistorySegment,
        ids: list[str],
        by_id: dict[str, Interaction],
    ) -> list[Message]:
        lines = [self._summary_line(by_id[ident]) for ident in ids]
        body: list[str] = []
        if segment.summary:
            body.append(f"Existing summary:\n{segment.summary}")
        body.append("Conversation:\n" + "\n".join(lines))
        body.append("Updated summary:")
        return [
            Message(role="system", content=SUMMARY_INSTRUCTIONS.strip()),
            Message(role="user", content="\n\n".join(body)),
        ]

    def _summary_line(self, item: Interaction) -> str:
        tz = timezone_name(self._settings)
        stamp = time_marker(item.ts, timezone_name=tz, now=now_ts())
        text = f"[{stamp}] user: {item.user_text}"
        if item.assistant_text:
            astamp = time_marker(item.assistant_ts or item.ts, timezone_name=tz, now=now_ts())
            text += f"\n[{astamp}] assistant: {item.assistant_text}"
        return text

    def _select_recent(
        self,
        items: list[Interaction],
        budget: int,
        *,
        chars: int,
        max_chars: int,
        gap_hours: float,
        tz: str,
        now: str,
        reason: str,
        prefix_ts: str | None = None,
    ) -> tuple[list[str], list[str], list[tuple[str, str]]]:
        if budget <= 0 or not items:
            return [], [], [(item.id, "over token budget") for item in items]
        accepted: list[str] = []
        included: list[str] = []
        dropped: list[tuple[str, str]] = []
        previous_ts: str | None = prefix_ts
        # Render oldest-to-newest so gap markers see the real previous message,
        # then select newest-first so the most recent exchange always survives.
        rendered: list[tuple[str, list[str]]] = []
        for item in items:
            lines, previous_ts = self._render(item, previous_ts, tz, now, max_chars, gap_hours)
            rendered.append((item.id, lines))
        used = 0
        pending = list(reversed(rendered))
        for index, (ident, lines) in enumerate(pending):
            block = "\n".join(lines)
            cost = estimate_tokens(block, chars_per_token=chars)
            if used + cost <= budget:
                accepted.insert(0, block)
                included.insert(0, ident)
                used += cost
                continue
            if not accepted:
                clipped = self._fit_text(block, budget, chars)
                if clipped:
                    accepted.insert(0, clipped)
                    included.insert(0, ident)
                else:
                    dropped.append((ident, reason + "; over token budget"))
            else:
                dropped.append((ident, reason + "; over token budget"))
            # Keep a contiguous newest window. Cherry-picking small, much older
            # messages after a large recent block would make gaps look smaller
            # than they are and would hide the actual cost of full history.
            for older_ident, _ in pending[index + 1 :]:
                dropped.append((older_ident, reason + "; over token budget"))
            break
        return accepted, included, dropped

    def _render(
        self,
        item: Interaction,
        previous_ts: str | None,
        tz: str,
        now: str,
        max_chars: int,
        gap_hours: float,
    ) -> tuple[list[str], str | None]:
        lines = [
            self._render_message("user", item.user_text, item.ts, previous_ts, tz, now, max_chars, gap_hours)
        ]
        last_ts = item.ts
        if item.assistant_text:
            lines.append(
                self._render_message(
                    "assistant",
                    item.assistant_text,
                    item.assistant_ts or item.ts,
                    last_ts,
                    tz,
                    now,
                    max_chars,
                    gap_hours,
                )
            )
            last_ts = item.assistant_ts or item.ts
        return [line for line in lines if line], last_ts

    def _render_message(
        self,
        role: str,
        text: str,
        ts: str,
        previous_ts: str | None,
        tz: str,
        now: str,
        max_chars: int,
        gap_hours: float,
    ) -> str:
        value = " ".join((text or "").split())
        if len(value) > max_chars:
            value = value[: max(0, max_chars - 1)].rstrip() + "…"
        marker = time_marker(ts, timezone_name=tz, now=now)
        gap = ""
        if previous_ts:
            seconds = gap_between(previous_ts, ts)
            if seconds >= gap_hours * 3600:
                gap = f"({human_gap(seconds)} later) "
        return f"{gap}[{marker}] {role}: {value}"

    def _segments(self, interactions: list[Interaction], mode: str) -> list[HistorySegment]:
        if mode == "summary":
            first = interactions[0]
            last = interactions[-1]
            return [
                HistorySegment(
                    id=self._rolling_id(first),
                    session_id=first.session_id,
                    started_at=first.ts,
                    ended_at=last.assistant_ts or last.ts,
                    interaction_ids=[item.id for item in interactions],
                )
            ]
        gap_s = float(self._settings.get("context.history.segment_gap_hours")) * 3600
        groups: list[list[Interaction]] = [[interactions[0]]]
        for item in interactions[1:]:
            previous = groups[-1][-1]
            previous_end = previous.assistant_ts or previous.ts
            if gap_between(previous_end, item.ts) > gap_s:
                groups.append([item])
            else:
                groups[-1].append(item)
        return [self._segment(group) for group in groups]

    def _segment(self, group: list[Interaction]) -> HistorySegment:
        first = group[0]
        last = group[-1]
        return HistorySegment(
            id=self._segment_id(first),
            session_id=first.session_id,
            started_at=first.ts,
            ended_at=last.assistant_ts or last.ts,
            interaction_ids=[item.id for item in group],
        )

    @staticmethod
    def _segment_id(item: Interaction) -> str:
        suffix = item.id.split("_", 1)[-1]
        return f"seg_{suffix}"

    @staticmethod
    def _rolling_id(item: Interaction) -> str:
        suffix = item.session_id.split("_", 1)[-1]
        return f"seg_rolling_{suffix}"

    def _summary_prefix(
        self, segment: HistorySegment, needed_ids: list[str]
    ) -> tuple[HistorySegment | None, int]:
        """Return a stored summary that covers a prefix of ``needed_ids``.

        The current segment grows after a summary is written, so exact coverage
        is a moving target. A covered prefix is still useful; the caller keeps
        the uncovered tail verbatim.
        """
        if not needed_ids:
            return None, 0
        stored = self._store.get(segment.id)
        if stored is None or not stored.summary or not stored.covered_ids:
            return None, 0
        covered = list(stored.covered_ids)
        if len(covered) > len(needed_ids) or covered != list(needed_ids[: len(covered)]):
            return None, 0
        return stored, len(covered)

    def _pending_turns(self) -> int:
        interactions = list(self._sessions.list_interactions(limit=None))
        if not interactions:
            return 0
        mode = str(self._settings.get("context.history.strategy") or "segment")
        segments = self._segments(interactions, mode)
        pending = 0
        for index, segment in enumerate(segments):
            target = len(segment.interaction_ids)
            if index == len(segments) - 1:
                target = max(0, target - max(0, int(self._settings.get("context.history.verbatim_turns"))))
            stored = self._store.get(segment.id)
            covered = len(stored.covered_ids) if stored is not None and stored.summary else 0
            pending += max(0, target - covered)
        return pending

    @staticmethod
    def _fit_text(text: str, tokens: int, chars: int) -> str:
        if tokens <= 0 or not text:
            return ""
        keep = max(0, tokens * max(1, chars))
        if len(text) <= keep:
            return text
        return text[:keep].rstrip()

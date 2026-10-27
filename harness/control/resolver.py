from __future__ import annotations

import re
from typing import Any

from harness.constants import DecisionIntent
from harness.contracts.decisions import Decision, DecisionInterpretation


_APPROVALS = {
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "go ahead",
    "do it",
    "proceed",
    "confirm",
    "confirmed",
    "approved",
    "approve",
    "sounds good",
    "yes please",
    "please do",
    "go for it",
    "absolutely",
    "correct",
}
_REJECTIONS = {
    "no",
    "nope",
    "nah",
    "no thanks",
    "no thank you",
    "don't",
    "do not",
    "don't do that",
    "do not do that",
    "reject",
    "decline",
    "negative",
    "not that",
}
_DEFERRALS = {
    "not yet",
    "later",
    "not now",
    "maybe later",
    "hold off",
    "hold on",
    "wait",
    "pause",
    "remind me later",
    "afterwards",
}
_CANCELLATIONS = {
    "stop",
    "stop that",
    "stop it",
    "cancel",
    "cancel that",
    "cancel it",
    "abort",
    "forget it",
    "never mind",
    "nevermind",
    "stop the task",
    "stop the work",
}
_AFFIRMATION_PREFIX = re.compile(
    r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|go ahead|do it|proceed|confirm(?:ed)?|"
    r"approved?|sounds good|absolutely|correct)\b",
    re.IGNORECASE,
)
_MODIFICATION_CUE = re.compile(
    r"\b(?:but|except|instead|without|only|skip|avoid|don't|do not|leave|"
    r"keep|change|update|use|with|rather than|apart from)\b",
    re.IGNORECASE,
)
_NEGATED_PHRASE = re.compile(
    r"\b(?:don't|do not|without|except|skip|avoid|rather than|instead of|not)\b\s+(.+?)"
    r"(?=(?:\s+(?:and|but|then|please)\b)|[.;]|$)",
    re.IGNORECASE,
)
_QUESTION_START = re.compile(
    r"^(?:what|why|how|when|where|who|which|whose|is|are|was|were|does|do|did|"
    r"can|could|would|will|should|may|might|tell me|explain)\b",
    re.IGNORECASE,
)
_NEW_REQUEST_START = re.compile(
    r"^(?:please\s+)?(?:check|ping|tell|remind|set|schedule|send|remember|find|get|"
    r"show|list|run|restart|start|create|make|add|remove|delete|update|open|close|"
    r"turn|book|order|search|look|read|write|call|play|summarize|help)\b",
    re.IGNORECASE,
)
# Questions that refer to the open decision set itself ("which one?", "which
# proposal?", "what are my options?") are requests for clarification. They are
# not new requests and must not be routed to the planner.
_DECISION_REFERENCE = re.compile(
    r"\b(?:which\s+(?:one|ones|option|options|decision|decisions|proposal|proposals|"
    r"action|actions)|which\s+of\s+(?:these|those|them)|"
    r"what\s+are\s+(?:my|the)\s+(?:options|choices|pending\s+(?:decisions|proposals))|"
    r"what(?:'s|\s+is)\s+pending)\b",
    re.IGNORECASE,
)
_STOPWORDS = {
    "the", "a", "an", "that", "this", "it", "to", "of", "for", "on", "in", "and",
    "or", "is", "are", "be", "please", "now", "then", "just", "really", "very",
    "can", "could", "would", "will", "should", "my", "your", "me", "you", "we",
    "first", "second", "third", "fourth", "last", "one", "two", "three",
}


def _normalize(text: str) -> str:
    lowered = text.strip().lower()
    lowered = re.sub(r"[\u2018\u2019]", "'", lowered)
    lowered = re.sub(r"[\u201c\u201d]", '"', lowered)
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered.strip(" \t\r\n.!?,;:\"'")


def _tokens(text: str) -> list[str]:
    return [
        item
        for item in re.findall(r"[a-z0-9][a-z0-9_-]*", _normalize(text))
        if len(item) >= 3 and item not in _STOPWORDS
    ]


class DefaultDecisionResolver:
    """Deterministic, context-aware reading of short user replies.

    The resolver never treats a keyword as authorization on its own. It first
    establishes which decision(s) the message could refer to, then classifies
    the reply. An ambiguous reply is reported as ambiguous, never approved.
    """

    def interpret(
        self,
        text: str,
        *,
        decisions: list[Decision],
        awaiting_question: bool = False,
    ) -> DecisionInterpretation:
        normalized = _normalize(text)
        if not normalized:
            return DecisionInterpretation(DecisionIntent.CLARIFY.value, reason="empty reply")

        if awaiting_question:
            referenced = self._select_explicit(text, decisions)
            if referenced is None:
                # A pending ask is a competing unresolved interaction. A bare
                # "yes"/"no" answers the question, not an unrelated proposal.
                # Only an explicit cancellation still acts on the decision log.
                if normalized in _CANCELLATIONS or self._starts_with_cancel(normalized):
                    return DecisionInterpretation(DecisionIntent.CANCEL.value)
                if "?" in text or _NEW_REQUEST_START.search(normalized):
                    return DecisionInterpretation(
                        DecisionIntent.UNRELATED.value,
                        decision_ids=[item.id for item in decisions],
                        reason="new input while a question is pending",
                    )
                return DecisionInterpretation(
                    DecisionIntent.NONE.value,
                    reason="answers a pending question",
                )
        else:
            referenced = self._select(text, decisions)

        if not decisions:
            if normalized in _CANCELLATIONS:
                return DecisionInterpretation(DecisionIntent.CANCEL.value)
            return DecisionInterpretation(DecisionIntent.NONE.value)

        ids = [item.id for item in decisions]
        ambiguous = referenced is None and len(decisions) > 1

        if self._is_question(text, decisions):
            return DecisionInterpretation(
                DecisionIntent.CLARIFY.value,
                decision_ids=ids,
                selected_id=referenced.id if referenced else None,
                ambiguous=ambiguous,
                reason="question rather than a decision",
            )
        if "?" in text:
            return DecisionInterpretation(
                DecisionIntent.UNRELATED.value,
                decision_ids=ids,
                reason="new question",
            )

        if normalized in _CANCELLATIONS or self._starts_with_cancel(normalized):
            return DecisionInterpretation(
                DecisionIntent.CANCEL.value,
                decision_ids=ids,
                selected_id=referenced.id if referenced else None,
                ambiguous=ambiguous,
            )

        if self._is_deferral(normalized):
            return DecisionInterpretation(
                DecisionIntent.DEFER.value,
                decision_ids=ids,
                selected_id=referenced.id if referenced else None,
                ambiguous=ambiguous,
            )

        if normalized in _REJECTIONS:
            return DecisionInterpretation(
                DecisionIntent.REJECT.value,
                decision_ids=ids,
                selected_id=referenced.id if referenced else None,
                ambiguous=ambiguous,
            )

        if _AFFIRMATION_PREFIX.search(normalized) and _MODIFICATION_CUE.search(normalized):
            return DecisionInterpretation(
                DecisionIntent.MODIFY.value,
                decision_ids=ids,
                selected_id=referenced.id if referenced else None,
                ambiguous=ambiguous,
                modification={
                    "text": text,
                    "exclusions": self._exclusions(text),
                },
            )

        if normalized in _APPROVALS or _AFFIRMATION_PREFIX.search(normalized):
            return DecisionInterpretation(
                DecisionIntent.APPROVE.value,
                decision_ids=ids,
                selected_id=referenced.id if referenced else None,
                ambiguous=ambiguous,
            )

        if _NEW_REQUEST_START.search(normalized):
            return DecisionInterpretation(
                DecisionIntent.UNRELATED.value,
                decision_ids=ids,
                reason="new request",
            )

        return DecisionInterpretation(
            DecisionIntent.CLARIFY.value,
            decision_ids=ids,
            selected_id=referenced.id if referenced else None,
            ambiguous=ambiguous,
            reason="reply does not clearly resolve a decision",
        )

    def _select(self, text: str, decisions: list[Decision]) -> Decision | None:
        explicit = self._select_explicit(text, decisions)
        if explicit is not None:
            return explicit
        if len(decisions) == 1:
            return decisions[0]
        return None

    def _select_explicit(self, text: str, decisions: list[Decision]) -> Decision | None:
        lowered = _normalize(text)
        for item in decisions:
            if item.id.lower() in lowered:
                return item
        matches = []
        for item in decisions:
            terms = self._terms(item)
            if terms and any(term in lowered for term in terms):
                matches.append(item)
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
        return self._select_ordinal(lowered, decisions)

    @staticmethod
    def _select_ordinal(lowered: str, decisions: list[Decision]) -> Decision | None:
        ordinals = {
            "first": 0,
            "1st": 0,
            "second": 1,
            "2nd": 1,
            "third": 2,
            "3rd": 2,
            "fourth": 3,
            "4th": 3,
        }
        words = set(re.findall(r"[a-z0-9]+", lowered))
        if "last" in words:
            return decisions[-1] if decisions else None
        indexes = {index for word, index in ordinals.items() if word in words}
        if len(indexes) == 1:
            index = indexes.pop()
            if 0 <= index < len(decisions):
                return decisions[index]
        return None

    @staticmethod
    def _terms(decision: Decision) -> set[str]:
        values = [decision.summary, decision.prompt]
        proposal = decision.proposal or {}
        for key in ("name", "capability", "operation"):
            if proposal.get(key):
                values.append(str(proposal[key]))
        arguments = proposal.get("arguments")
        if isinstance(arguments, dict):
            values.extend(str(value) for value in arguments.values() if isinstance(value, str))
        for step in proposal.get("steps") or []:
            if not isinstance(step, dict):
                continue
            for key in ("capability", "operation", "goal", "summary"):
                if step.get(key):
                    values.append(str(step[key]))
            args = step.get("arguments")
            if isinstance(args, dict):
                values.extend(str(value) for value in args.values() if isinstance(value, str))
        terms: set[str] = set()
        for value in values:
            for token in _tokens(value.replace("_", " ").replace("-", " ")):
                if token not in {"run", "create", "domain", "action", "confirmation"}:
                    terms.add(token)
        return terms

    def _is_question(self, text: str, decisions: list[Decision]) -> bool:
        stripped = text.strip()
        if "?" not in stripped:
            return False
        normalized = _normalize(stripped)
        # "yes?" is not approval: it asks the harness to disambiguate.
        if normalized in _APPROVALS:
            return True
        if not _QUESTION_START.search(normalized):
            return False
        if _DECISION_REFERENCE.search(normalized):
            return True
        if normalized in {"what", "why", "how", "which", "really", "huh"}:
            return True
        if self._select_explicit(stripped, decisions) is not None:
            return True
        decision_terms = set().union(*(self._terms(item) for item in decisions)) if decisions else set()
        lowered = f" {normalized} "
        if any(term in lowered for term in decision_terms):
            return True
        if any(
            word in lowered
            for word in (
                " that ",
                " this ",
                " it ",
                " proposal ",
                " plan ",
                " action ",
                " decision ",
                " proceed ",
                " confirm ",
            )
        ):
            return True
        # A standalone factual question is a new request, not a decision.
        return False

    @staticmethod
    def _starts_with_cancel(normalized: str) -> bool:
        return normalized.startswith(("cancel ", "stop "))

    @staticmethod
    def _is_deferral(normalized: str) -> bool:
        if normalized in _DEFERRALS:
            return True
        return any(
            phrase in normalized
            for phrase in ("not yet", "not right now", "maybe later", "hold off", "remind me later")
        )

    def _exclusions(self, text: str) -> list[str]:
        found: list[str] = []
        for match in _NEGATED_PHRASE.finditer(text):
            phrase = match.group(1).strip(" .,;:!?\"'")
            if phrase and phrase not in found:
                found.append(phrase)
        return found

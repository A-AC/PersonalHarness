from __future__ import annotations

import pytest

from harness.orchestrator.parse import (
    PROPOSAL_SCHEMA_LOOSE,
    PROPOSAL_SCHEMA_STRICT,
    parse_proposal,
)


def _variant(kind: str) -> dict:
    return next(item for item in PROPOSAL_SCHEMA_STRICT["oneOf"] if item["properties"]["kind"]["const"] == kind)


def test_strict_schema_requires_the_fields_each_action_needs() -> None:
    assert _variant("respond")["required"] == ["kind", "message"]
    assert _variant("ask")["required"] == ["kind", "question", "missing"]
    assert _variant("act")["required"] == ["kind", "capability", "operation"]
    assert _variant("continue")["required"] == ["kind", "purpose"]
    assert _variant("end")["required"] == ["kind", "status", "summary"]
    assert PROPOSAL_SCHEMA_LOOSE["required"] == ["kind"]


def test_parse_errors_name_every_missing_field_at_once() -> None:
    with pytest.raises(ValueError) as exc:
        parse_proposal('{"kind":"act"}')
    message = str(exc.value)
    assert "capability" in message
    assert "operation" in message


def test_parse_errors_disclose_allowed_kinds_and_end_statuses() -> None:
    with pytest.raises(ValueError, match='unknown kind "nope"; allowed: respond, ask, act, continue, end'):
        parse_proposal('{"kind":"nope"}')
    with pytest.raises(ValueError, match='invalid end.status "done"; allowed: completed, cancelled, abandoned'):
        parse_proposal('{"kind":"end","status":"done","summary":"x"}')

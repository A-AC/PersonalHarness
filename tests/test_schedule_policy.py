from __future__ import annotations

import datetime as dt

import pytest

from harness.schedule_policy import (
    allowed_tools,
    authorizes_web_search,
    render_query,
    validate_schedule_request,
    validate_unattended,
)


NOW = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc)


def _policy(**changes):
    value = {
        "web_queries": [{
            "query": "frontier AI news {current_year}",
            "max_results": 5,
            "time_range": "week",
        }],
        "allow_task_notes": False,
    }
    value.update(changes)
    return validate_unattended(value)


def test_policy_normalizes_and_matches_only_approved_template():
    policy = _policy()
    assert authorizes_web_search(policy, {
        "query": "frontier AI news 2026", "time_range": "week",
    }, NOW)
    assert not authorizes_web_search(policy, {
        "query": "frontier AI news 2024", "time_range": "week",
    }, NOW)
    assert not authorizes_web_search(policy, {
        "query": "frontier AI news 2026", "time_range": "year",
    }, NOW)
    assert not authorizes_web_search(policy, {
        "query": "sensitive calendar details", "time_range": "week",
    }, NOW)
    assert allowed_tools(policy) == {"web_search"}


def test_policy_supports_explicit_bounded_notes_capability():
    policy = _policy(allow_task_notes=True)
    assert allowed_tools(policy) == {"web_search", "update_task_notes"}


@pytest.mark.parametrize("value", [
    [],
    {"web_queries": []},
    {"web_queries": [{"query": "news {user_input}"}]},
    {"web_queries": [{"query": "news", "time_range": "hour"}]},
    {"web_queries": [{"query": "news", "max_results": 100}]},
    {"web_queries": [{"query": "news"}], "allow_task_notes": "yes"},
    {"web_queries": [{"query": "news"}], "delegate_task": True},
])
def test_policy_fails_closed(value):
    with pytest.raises(ValueError):
        validate_unattended(value)


@pytest.mark.parametrize("arguments", [
    {"kind": "daily"},
    {"kind": "weekly", "time_of_day": "12:00"},
    {"kind": "interval"},
    {"kind": "once"},
])
def test_schedule_request_is_validated_before_approval(arguments):
    with pytest.raises(ValueError):
        validate_schedule_request(arguments)


def test_date_placeholders_are_deterministic():
    assert render_query("AI news {current_date} {current_month} {current_year}", NOW) == (
        "AI news 2026-11-18 2026-11 2026"
    )

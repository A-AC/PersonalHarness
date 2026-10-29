"""Time handling: the host system clock is the default everywhere.

Stored timestamps stay UTC. Everything a person or the model reads (context
clock, schedules, UI, CLI) is rendered in the host's local time. A naive
user-supplied instant means local time, not UTC.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
import yaml

from harness import util
from harness.config import (
    Config,
    ConfigError,
    format_local,
    load_config,
    local_tz,
    resolve_zone,
    validate_group_update,
)


def _same_offset(left, right) -> bool:
    now = dt.datetime.now(dt.timezone.utc)
    return now.astimezone(left).utcoffset() == now.astimezone(right).utcoffset()


def test_default_timezone_follows_the_system_clock(tmp_path):
    config = load_config(tmp_path)
    assert config.get("timezone") == "system"
    assert _same_offset(local_tz(config), util.system_tz())


def test_format_local_renders_utc_in_system_time():
    config = Config({"timezone": "system"}, None)
    stamp = "2026-01-01T12:00:00Z"
    expected = util.parse_iso(stamp).astimezone(util.system_tz()).strftime(
        "%Y-%m-%d %H:%M"
    )
    assert format_local(stamp, config) == expected


def test_unknown_timezone_falls_back_to_system_not_utc():
    zone = resolve_zone("Not/ARealZone")
    assert _same_offset(zone, util.system_tz())


def test_context_clock_is_local_time(make_app):
    app = make_app.build(default="ok")
    task = app.tasks.create(objective="check the clock", origin="user")
    built = app.context.build(
        task=task, domain=None, trigger={"kind": "user"},
    )
    line = next(
        row for row in built["system"].splitlines() if row.startswith("Time:")
    )
    now = dt.datetime.now(local_tz(app.config))
    assert now.strftime("%Y-%m-%d") in line
    assert now.strftime("%z") in line
    # The old bug: a UTC ISO timestamp was labelled as the local timezone.
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", line)


def test_naive_at_ts_is_interpreted_as_system_local(make_app):
    app = make_app.build(default="ok")
    naive = "2031-06-15T09:30:00"
    schedule = app.scheduler.create_schedule(
        name="local-once", prompt="x", kind="once", at_ts=naive,
    )
    expected = (
        dt.datetime.fromisoformat(naive)
        .replace(tzinfo=util.system_tz())
        .astimezone(dt.timezone.utc)
    )
    assert util.parse_iso(schedule["next_due_at"]) == expected


def test_at_ts_with_an_offset_is_respected(make_app):
    app = make_app.build(default="ok")
    explicit = "2031-06-15T09:30:00+02:00"
    schedule = app.scheduler.create_schedule(
        name="offset-once", prompt="x", kind="once", at_ts=explicit,
    )
    expected = dt.datetime.fromisoformat(explicit).astimezone(dt.timezone.utc)
    assert util.parse_iso(schedule["next_due_at"]) == expected


def test_schedule_defaults_to_system_timezone(make_app):
    app = make_app.build(default="ok")
    schedule = app.scheduler.create_schedule(
        name="daily-local", prompt="x", kind="daily", time_of_day="09:00",
    )
    assert schedule["tz"] == "system"
    due_local = util.parse_iso(schedule["next_due_at"]).astimezone(
        util.system_tz()
    )
    assert (due_local.hour, due_local.minute) == (9, 0)


def test_timezone_validation_accepts_system_and_iana(tmp_path):
    config = load_config(tmp_path)
    assert validate_group_update(config, "timezone", "system") == "system"
    assert validate_group_update(config, "timezone", "") == "system"
    assert validate_group_update(config, "timezone", "UTC") == "UTC"
    with pytest.raises(ConfigError):
        validate_group_update(config, "timezone", "Mars/Olympus")


def test_web_page_renders_timestamps_in_browser_local_time(make_app, client):
    app = make_app.build(default="ok")
    http = client(app)
    page = http.get("/")
    assert page.status_code == 200
    assert '<script src="/assets/app.js"></script>' in page.text
    script = http.get("/assets/app.js")
    assert script.status_code == 200
    assert "new Date(ts)" in script.text
    assert "getFullYear" in script.text
    assert "system_timezone" in script.text


def test_state_reports_the_detected_system_timezone(make_app, client):
    app = make_app.build(default="ok")
    payload = client(app).get("/api/state").json()
    assert payload["system_timezone"]
    assert payload["config"]["timezone"] == "system"


def test_old_hardcoded_default_timezone_migrates_to_system(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        "timezone: America/Mexico_City\n", encoding="utf-8"
    )
    config = load_config(data_dir)
    assert config.get("timezone") == "system"
    # The file is rewritten, so the migration survives the next start.
    on_disk = yaml.safe_load(
        (data_dir / "config.yaml").read_text(encoding="utf-8")
    )
    assert on_disk["timezone"] == "system"


def test_a_deliberate_timezone_choice_is_preserved(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        "timezone: Europe/Berlin\n", encoding="utf-8"
    )
    config = load_config(data_dir)
    assert config.get("timezone") == "Europe/Berlin"

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from harness.events.log import FileEventLog
from harness.settings.service import FileSettingsService, SettingsError


def make_service(tmp_path: Path, payload: dict | None = None, env: dict | None = None) -> FileSettingsService:
    if payload is not None:
        (tmp_path / "settings.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    return FileSettingsService(tmp_path / "settings.yaml", env=env or {})


def test_defaults_are_written_and_readable(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    assert (tmp_path / "settings.yaml").exists()
    assert service.get("loop.max_iterations") == 5
    assert service.get("capabilities.policy.write") == "deny"
    assert service.get("context.budgets.total") == 4000
    # Experiment-backed defaults: skip the planner for short single requests
    # and stop after a non-retryable execution failure. Reasoning stays on
    # because disabling it lost missing-argument clarification.
    assert service.get("tasks.planning_mode") == "heuristic"
    assert service.get("loop.stop_after_nonretryable_failure") is True
    assert service.get("models.interactive.enable_thinking") is True
    # V1.1 real-use evidence: the strict per-kind action schema avoids
    # incomplete actions, and the full prompt beat the trimmed one.
    assert service.get("models.interactive.action_schema") == "strict"
    assert service.get("models.interactive.response_format") == "json_schema"
    assert service.get("context.prompt_variant") == "full"
    assert service.get("context.include_time") is True
    assert service.get("loop.honesty_guard") is True
    assert service.get("loop.internal_schema_ask_guard") is True
    assert service.get("capabilities.coerce_optional_arguments") is True
    assert service.get("capabilities.disclose_enum_values") is True
    assert service.get("capabilities.reject_placeholders") is True


def test_unknown_file_key_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="unknown setting"):
        make_service(tmp_path, {"loop": {"max_iterationz": 3}})


def test_out_of_bounds_and_enum_validation(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(SettingsError):
        service.set("loop.max_iterations", 0, "test")
    with pytest.raises(SettingsError):
        service.set("capabilities.policy.write", "sometimes", "test")
    with pytest.raises(SettingsError):
        service.set("unknown.key", 1, "test")
    assert service.get("loop.max_iterations") == 5


def test_set_reset_and_history_emit_events(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    events = FileEventLog(tmp_path / "events", service)
    service.attach_events(events)
    change = service.set("context.budgets.history", 123, "user")
    assert change.old_value == 800
    assert service.get("context.budgets.history") == 123
    service.reset("context.budgets.history", "user")
    assert service.get("context.budgets.history") == 800
    history = service.history("context.budgets.history")
    assert len(history) == 2
    assert events.query(operation="settings.set")
    assert events.query(actor="user")


def test_env_overrides_are_validated(tmp_path: Path) -> None:
    service = make_service(
        tmp_path,
        env={
            "HARNESS_BIND_PORT": "9090",
            "HARNESS_INTERACTIVE_ENDPOINT": "http://example.invalid/v1/chat/completions",
            "HARNESS_DATA_DIR": "ignored-by-settings",
        },
    )
    assert service.get("ui.bind_port") == 9090
    assert service.get("models.interactive.endpoint").startswith("http://example.invalid")
    with pytest.raises(SettingsError):
        make_service(tmp_path, env={"HARNESS_BIND_PORT": "not-a-port"})


def test_nullable_seed_can_be_reset(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.set("models.interactive.seed", 42, "user")
    assert service.get("models.interactive.seed") == 42
    service.set("models.interactive.seed", "", "user")
    assert service.get("models.interactive.seed") is None

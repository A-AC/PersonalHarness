"""Load readable YAML/JSON scenario definitions.

A file can contain one scenario mapping, a list of scenarios, or a top-level
``scenarios:`` list. The loader validates stable IDs and step shapes early so a
malformed suite fails before any harness state is written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import yaml

from harness.evaluation.models import Scenario, ScenarioStep


DEFAULT_SCENARIO_DIR = Path(__file__).resolve().parents[2] / "eval" / "scenarios"
VALID_MODES = {"deterministic", "live"}
VALID_STEP_KINDS = {
    "turn",
    "inspect",
    "restart",
    "tick",
    "sleep",
    "review",
    "skill",
    "learning_signal",
}
VALID_SEVERITIES = {"critical", "high", "medium", "low"}


class ScenarioError(ValueError):
    pass


def deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def _read_document(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _scenario_entries(document: Any, path: Path) -> list[dict[str, Any]]:
    if document is None:
        return []
    if isinstance(document, dict) and "scenarios" in document:
        entries = document["scenarios"]
        defaults = document.get("defaults") or {}
    elif isinstance(document, list):
        entries = document
        defaults = {}
    elif isinstance(document, dict):
        entries = [document]
        defaults = {}
    else:
        raise ScenarioError(f"{path}: expected a scenario mapping or list")
    if not isinstance(entries, list):
        raise ScenarioError(f"{path}: 'scenarios' must be a list")
    parsed: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            raise ScenarioError(f"{path}: every scenario must be a mapping")
        merged = deep_merge(
            json.loads(json.dumps(defaults)), json.loads(json.dumps(item))
        )
        parsed.append(merged)
    return parsed


def _require_text(payload: dict[str, Any], key: str, where: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ScenarioError(f"{where}: '{key}' is required")
    return value


def parse_scenario(payload: dict[str, Any], *, source: str = "") -> Scenario:
    identifier = _require_text(payload, "id", source or "scenario")
    where = f"{source or 'scenario'} [{identifier}]"
    name = _require_text(payload, "name", where)
    category = _require_text(payload, "category", where)
    severity = str(payload.get("severity") or "medium").strip().lower()
    if severity not in VALID_SEVERITIES:
        raise ScenarioError(f"{where}: severity must be one of {sorted(VALID_SEVERITIES)}")

    raw_modes = payload.get("modes") or ["deterministic"]
    if not isinstance(raw_modes, list):
        raise ScenarioError(f"{where}: 'modes' must be a list")
    modes = [str(item).strip().lower() for item in raw_modes]
    unknown = set(modes) - VALID_MODES
    if unknown:
        raise ScenarioError(f"{where}: unknown modes {sorted(unknown)}")
    if not modes:
        raise ScenarioError(f"{where}: at least one mode is required")

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ScenarioError(f"{where}: 'steps' must be a non-empty list")
    steps: list[ScenarioStep] = []
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise ScenarioError(f"{where}: step {index} must be a mapping")
        kind = str(raw.get("kind") or "turn").strip().lower()
        if kind not in VALID_STEP_KINDS:
            raise ScenarioError(
                f"{where}: step {index} has unknown kind {kind!r}; "
                f"expected one of {sorted(VALID_STEP_KINDS)}"
            )
        user = raw.get("input")
        if kind == "turn" and not str(user or "").strip():
            raise ScenarioError(f"{where}: turn step {index} needs 'input'")
        script = raw.get("script")
        if script is not None and not isinstance(script, list):
            raise ScenarioError(f"{where}: step {index} 'script' must be a list or null")
        expect = raw.get("expect") or {}
        if not isinstance(expect, dict):
            raise ScenarioError(f"{where}: step {index} 'expect' must be a mapping")
        steps.append(
            ScenarioStep(
                kind=kind,
                input=str(user).strip() if user is not None else None,
                script=script,
                expect=expect,
                ticks=int(raw.get("ticks") or 1),
                seconds=float(raw.get("seconds") or 0.0),
                name=str(raw.get("name")).strip() if raw.get("name") else None,
                action=str(raw.get("action")).strip() if raw.get("action") else None,
                target=str(raw.get("target")).strip() if raw.get("target") else None,
            )
        )

    fixtures = payload.get("fixtures") or {}
    settings = payload.get("settings") or {}
    if not isinstance(fixtures, dict):
        raise ScenarioError(f"{where}: 'fixtures' must be a mapping")
    if not isinstance(settings, dict):
        raise ScenarioError(f"{where}: 'settings' must be a mapping")
    cleanup = str(payload.get("cleanup") or "retain").strip().lower()
    if cleanup not in {"retain", "delete"}:
        raise ScenarioError(f"{where}: cleanup must be 'retain' or 'delete'")

    return Scenario(
        id=identifier,
        name=name,
        category=category,
        severity=severity,
        description=str(payload.get("description") or "").strip(),
        modes=modes,
        tags=[str(item) for item in (payload.get("tags") or [])],
        settings=settings,
        fixtures=fixtures,
        steps=steps,
        cleanup=cleanup,
        source=source,
    )


def load_scenario_files(paths: Iterable[str | Path]) -> list[Scenario]:
    scenarios: list[Scenario] = []
    seen: dict[str, str] = {}
    for item in paths:
        path = Path(item)
        if path.is_dir():
            files = sorted(path.glob("*.yaml")) + sorted(path.glob("*.yml")) + sorted(path.glob("*.json"))
        else:
            files = [path]
        for file in files:
            try:
                document = _read_document(file)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                raise ScenarioError(f"{file}: cannot read scenario file: {exc}") from exc
            for raw in _scenario_entries(document, file):
                scenario = parse_scenario(raw, source=str(file))
                if scenario.id in seen:
                    raise ScenarioError(
                        f"duplicate scenario id {scenario.id!r} in {file} and {seen[scenario.id]}"
                    )
                seen[scenario.id] = str(file)
                scenarios.append(scenario)
    if not scenarios:
        raise ScenarioError(f"no scenarios found in {[str(item) for item in paths]}")
    return scenarios


def load_default_scenarios(directory: str | Path | None = None) -> list[Scenario]:
    root = Path(directory) if directory is not None else DEFAULT_SCENARIO_DIR
    return load_scenario_files([root])


def select_scenarios(
    scenarios: list[Scenario],
    *,
    scenario_ids: str | None = None,
    categories: str | None = None,
    tags: str | None = None,
    mode: str | None = None,
) -> list[Scenario]:
    def keys(value: str | None) -> list[str]:
        return [item.strip().lower() for item in (value or "").split(",") if item.strip()]

    wanted_ids = keys(scenario_ids)
    wanted_categories = keys(categories)
    wanted_tags = keys(tags)
    selected = []
    for scenario in scenarios:
        if mode is not None and not scenario.supports(mode):
            continue
        if wanted_ids and not any(
            key == scenario.id.lower() or key in scenario.id.lower() for key in wanted_ids
        ):
            continue
        if wanted_categories and not any(
            key == scenario.category.lower() or key in scenario.category.lower()
            for key in wanted_categories
        ):
            continue
        lower_tags = {item.lower() for item in scenario.tags}
        if wanted_tags and not set(wanted_tags) & lower_tags:
            continue
        selected.append(scenario)
    return selected

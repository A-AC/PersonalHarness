"""Load, validate, and select YAML/JSON scenario files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

import yaml

from .models import MODES, STEP_KINDS, Scenario, ScenarioStep

DEFAULT_SCENARIO_DIR = Path("eval") / "scenarios"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$")


class ScenarioError(ValueError):
    pass


def load_scenarios(path: Path | str | None = None) -> list[Scenario]:
    """Load every ``*.yaml``/``*.yml``/``*.json`` file in a directory.

    Files are read in sorted order so reports are stable. A scenario file may
    contain a top-level ``scenarios:`` list or a single mapping.
    """
    root = Path(path or DEFAULT_SCENARIO_DIR)
    if root.is_file():
        files = [root]
    elif root.is_dir():
        files = sorted(
            item for item in root.iterdir()
            if item.suffix.lower() in (".yaml", ".yml", ".json")
        )
    else:
        raise ScenarioError(f"scenario path does not exist: {root}")
    if not files:
        raise ScenarioError(f"no scenario files under {root}")

    scenarios: list[Scenario] = []
    seen: dict[str, str] = {}
    for file in files:
        for raw in _read_file(file):
            scenario = _coerce(raw, file)
            if scenario.id in seen:
                raise ScenarioError(
                    f"duplicate scenario id {scenario.id!r} in "
                    f"{scenario.source} and {seen[scenario.id]}"
                )
            seen[scenario.id] = scenario.source
            scenarios.append(scenario)
    return scenarios


def select_scenarios(
    scenarios: Iterable[Scenario],
    *,
    mode: str | None = None,
    ids: str | None = None,
    category: str | None = None,
    scenario_tag: str | None = None,
) -> list[Scenario]:
    """Filter by mode, comma-separated id patterns, category, or tag."""
    patterns = [
        part.strip() for part in (ids or "").split(",") if part.strip()
    ]
    selected: list[Scenario] = []
    for scenario in scenarios:
        if mode and not scenario.supports(mode):
            continue
        if category and scenario.category != category:
            continue
        if scenario_tag and scenario_tag not in scenario.tags:
            continue
        if patterns and not any(_id_matches(scenario.id, p) for p in patterns):
            continue
        selected.append(scenario)
    if not selected:
        raise ScenarioError("no scenarios matched the selection")
    return selected


def _id_matches(scenario_id: str, pattern: str) -> bool:
    if any(char in pattern for char in "*?["):
        from fnmatch import fnmatchcase

        return fnmatchcase(scenario_id, pattern)
    return scenario_id == pattern


def _read_file(file: Path) -> list[dict[str, Any]]:
    text = file.read_text(encoding="utf-8")
    if file.suffix.lower() == ".json":
        import json

        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    if data is None:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and "scenarios" in data:
        scenarios = data.get("scenarios")
        if not isinstance(scenarios, list):
            raise ScenarioError(f"{file}: scenarios must be a list")
        return [item for item in scenarios if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    raise ScenarioError(f"{file}: expected a mapping or a list")


def _coerce(raw: dict[str, Any], file: Path) -> Scenario:
    scenario_id = str(raw.get("id") or "").strip()
    if not scenario_id or not _ID_RE.match(scenario_id):
        raise ScenarioError(f"{file}: scenario needs a valid 'id'")
    name = str(raw.get("name") or scenario_id).strip()
    category = str(raw.get("category") or "uncategorized").strip()
    modes = raw.get("modes") or ["deterministic"]
    if not isinstance(modes, list) or any(m not in MODES for m in modes):
        raise ScenarioError(
            f"{scenario_id}: modes must be a subset of {', '.join(MODES)}"
        )
    settings = raw.get("settings") or {}
    fixtures = raw.get("fixtures") or {}
    if not isinstance(settings, dict):
        raise ScenarioError(f"{scenario_id}: settings must be a mapping")
    if not isinstance(fixtures, dict):
        raise ScenarioError(f"{scenario_id}: fixtures must be a mapping")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ScenarioError(f"{scenario_id}: steps must be a non-empty list")
    steps = [_coerce_step(item, scenario_id, index, file)
             for index, item in enumerate(raw_steps)]
    tags = raw.get("tags") or []
    return Scenario(
        id=scenario_id,
        name=name,
        category=category,
        severity=str(raw.get("severity") or "medium").strip() or "medium",
        description=str(raw.get("description") or "").strip(),
        modes=[str(mode) for mode in modes],
        tags=[str(tag) for tag in tags] if isinstance(tags, list) else [str(tags)],
        settings=settings,
        fixtures=fixtures,
        steps=steps,
        cleanup=str(raw.get("cleanup") or "reset"),
        source=str(file),
    )


def _coerce_step(
    raw: Any, scenario_id: str, index: int, file: Path
) -> ScenarioStep:
    if not isinstance(raw, dict):
        raise ScenarioError(
            f"{scenario_id}: step {index} must be a mapping"
        )
    kind = str(raw.get("kind") or "turn").strip()
    if kind not in STEP_KINDS:
        raise ScenarioError(
            f"{scenario_id}: step {index} has unknown kind {kind!r}"
        )
    expect = raw.get("expect") or {}
    if not isinstance(expect, dict):
        raise ScenarioError(f"{scenario_id}: step {index} expect must be a mapping")
    reserved = {"kind", "name", "input", "script", "expect"}
    options = {key: value for key, value in raw.items() if key not in reserved}
    script = raw.get("script")
    if script is not None and not isinstance(script, list):
        raise ScenarioError(f"{scenario_id}: step {index} script must be a list")
    return ScenarioStep(
        kind=kind,
        name=str(raw.get("name") or "").strip(),
        input=str(raw.get("input") or ""),
        script=script,
        expect=expect,
        options=options,
    )

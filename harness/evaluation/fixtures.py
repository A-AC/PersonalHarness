"""Create scenario fixtures through the production managers.

Fixtures only use public manager methods, so a scenario starts from exactly
the state the harness would produce itself. Nothing here bypasses validation,
and nothing writes outside the scenario data directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .. import util


def apply_fixtures(app, fixtures: dict[str, Any] | None) -> dict[str, Any]:
    fixtures = fixtures or {}
    created: dict[str, Any] = {"domains": {}, "memories": [], "tasks": [],
                               "skills": [], "schedules": [], "automations": []}

    domain_ids: dict[str, str] = {}
    for spec in fixtures.get("domains", []) or []:
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        existing = app.domains.by_name(name)
        if existing:
            domain = existing
        else:
            domain = app.domains.create(
                str(spec.get("id") or util.new_id("dom")),
                name=name,
                description=str(spec.get("description") or ""),
                created_by="eval",
                seed_note=spec.get("seed_note"),
            )
        domain_ids[name] = domain["id"]
        domain_ids[domain["id"]] = domain["id"]
        created["domains"][name] = domain["id"]

    for spec in fixtures.get("memories", []) or []:
        content = str(spec.get("content") or "").strip()
        if not content:
            continue
        scope = spec.get("scope")
        domain_id = _domain_id(spec, domain_ids)
        if domain_id and not scope:
            scope = f"domain:{domain_id}"
        result = app.memory.remember(
            content,
            kind=spec.get("kind"),
            scope=scope,
            domain_id=domain_id,
            origin=str(spec.get("origin") or "user"),
            provenance={"source": "eval_fixture"},
            tags=list(spec.get("tags") or []),
            domains=app.domains,
        )
        created["memories"].append(result["memory"]["id"])

    for spec in fixtures.get("tasks", []) or []:
        objective = str(spec.get("objective") or "").strip()
        if not objective:
            continue
        task = app.tasks.create(
            objective=objective,
            domain_id=_domain_id(spec, domain_ids),
            origin=str(spec.get("origin") or "eval"),
            meta={"notes": str(spec.get("notes") or "")},
        )
        created["tasks"].append(task["id"])

    for spec in fixtures.get("skills", []) or []:
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        manager = app.modules.skills
        domains = [
            domain_ids.get(str(item), str(item))
            for item in (spec.get("domains") or [])
        ]
        module = manager.create_draft(
            name=name,
            description=str(spec.get("description") or name),
            preconditions=list(spec.get("preconditions") or []),
            domains=domains,
            body=str(spec.get("body") or ""),
            origin=str(spec.get("origin") or "import"),
            provenance={"source": "eval_fixture"},
            created_by=str(spec.get("created_by") or "user"),
            reason="evaluation fixture",
        )
        status = str(spec.get("status") or "active")
        if status == "active":
            module = manager.activate(module.id)
        elif status == "disabled":
            module = manager.disable(module.id)
        created["skills"].append(module.id)

    for spec in fixtures.get("schedules", []) or []:
        schedule = app.scheduler.create_schedule(
            name=str(spec.get("name") or "fixture schedule"),
            prompt=str(spec.get("prompt") or "fixture prompt"),
            kind=str(spec.get("kind") or "once"),
            at_ts=spec.get("at_ts"),
            interval_s=spec.get("interval_s"),
            time_of_day=spec.get("time_of_day"),
            weekday=spec.get("weekday"),
            domain_id=_domain_id(spec, domain_ids),
        )
        created["schedules"].append(schedule["id"])

    for spec in fixtures.get("automations", []) or []:
        rule = app.inbox.create_automation(
            str(spec.get("name") or "fixture rule"),
            enabled=bool(spec.get("enabled", True)),
            source=spec.get("source"),
            kind=spec.get("kind"),
            match_keywords=spec.get("match_keywords"),
            match_regex=spec.get("match_regex"),
            action=spec.get("action"),
            prompt_template=str(spec.get("prompt_template") or ""),
            domain_id=_domain_id(spec, domain_ids),
            cooldown_s=float(spec.get("cooldown_s") or 0.0),
            max_per_hour=spec.get("max_per_hour"),
        )
        created["automations"].append(rule["id"])

    servers = fixtures.get("mcp_servers") or []
    if servers:
        created["mcp_servers"] = _install_mcp(app, servers)

    return created


def _domain_id(spec: dict[str, Any], domain_ids: dict[str, str]) -> str | None:
    value = spec.get("domain") or spec.get("domain_id")
    if not value:
        return None
    return domain_ids.get(str(value), str(value))


def _install_mcp(app, servers: list[dict[str, Any]]) -> list[str]:
    """Write the import file and let the registry import it.

    Entries may use ``{root}`` for the repository root so an example server can
    be referenced without hard-coding a machine path.
    """
    root = Path(__file__).resolve().parents[2]
    entries: list[dict[str, Any]] = []
    for spec in servers:
        entry = dict(spec)
        args = [
            str(arg).replace("{root}", str(root)).replace("{python}", __import__("sys").executable)
            for arg in entry.get("args") or []
        ]
        entry["args"] = args
        entry["command"] = str(entry.get("command") or __import__("sys").executable).replace(
            "{python}", __import__("sys").executable
        )
        entries.append(entry)
    path = Path(app.data_dir) / "eval_mcp_servers.yaml"
    path.write_text(
        yaml.safe_dump({"servers": entries}, sort_keys=False),
        encoding="utf-8",
    )
    app.config.set("tools.mcp_servers_file", str(path))
    imported = app.modules.loader.import_legacy()
    app.modules.load_all()
    return imported

"""Skill modules: import, disclosure, lifecycle, and approval-gated drafts.

Skills are capability modules whose contribution is instruction text only.
They never register tools, schemas, or executable content; the runtime still
enforces every action through the ordinary tool/permission path.

Three seams live here:

- :class:`SkillManager` imports ``tools.skills_dir`` (``SKILL.md`` plus
  front matter) into ``kind=skill`` module rows, discloses active skills for
  one domain/objective, and owns the ``draft -> active -> disabled``
  lifecycle with versioned bodies and rollback.
- :class:`SkillBuilder` is the heartbeat pass that may turn a batch of
  successfully completed tool-using tasks into at most one draft skill.
- ``parse_front_matter`` keeps the import format small and deterministic.

The database row is authoritative. Bodies are rendered files under
``modules/skills/<id>/v<n>.md`` whose identity is owned by the row.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from .. import util
from ..model import ModelResponseError, ModelUnavailable
from .types import Module, ModuleBinding

SKILL_PREFIX = "skill:"
MAX_NAME_CHARS = 80
MAX_DESCRIPTION_CHARS = 500
MAX_PRECONDITIONS = 12
MAX_PRECONDITION_CHARS = 40
MAX_DOMAINS = 12
# A disclosed skill is instruction text, not the whole stored body.
DISCLOSURE_BODY_CHARS = 2000
HISTORY_LIMIT = 20
AUDIT_LIMIT = 50
# Only these identities may refine existing agent-owned skills. Imported and
# user-created skills stay read-only to the agent; the user may still edit
# them through the UI/API.
AGENT_SKILL_CREATORS = frozenset({"model", "builder"})

_LIST_FIELDS = ("preconditions", "domains")
_FRONT_MATTER_FIELDS = ("name", "description", "preconditions", "domains")


def _as_list(value: Any) -> list[str]:
    """Accept a comma string or a YAML-ish list and return clean strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        text = str(value).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        items = text.split(",")
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = str(item).strip().strip("'\"")
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Parse the minimal front matter used by imported ``SKILL.md`` files.

    Only ``key: value`` lines are supported. List fields accept either
    ``a, b`` or ``[a, b]``. A file without a complete front-matter block is
    treated as a body-only skill.
    """
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.splitlines()
    try:
        end = lines[1:].index("---") + 1
    except ValueError:
        return {}, text.strip()
    meta: dict[str, Any] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key not in _FRONT_MATTER_FIELDS:
            continue
        if key in _LIST_FIELDS:
            meta[key] = _as_list(value)
        else:
            meta[key] = value.strip()
    return meta, "\n".join(lines[end + 1:]).strip()


def skill_module_id(name: str) -> str:
    """Stable module id for a skill display name (``skill:<slug>``)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower())
    slug = slug.strip("-")[:60].strip("-")
    if not slug:
        raise ValueError("skill name must contain at least one letter or digit")
    return f"{SKILL_PREFIX}{slug}"


def _safe_dir(module_id: str) -> str:
    raw = str(module_id).split(":", 1)[-1].lower()
    safe = re.sub(r"[^a-z0-9_-]+", "_", raw).strip("_")
    return safe or "skill"


class SkillManager:
    """Import, disclose, and version skills stored as module rows."""

    def __init__(self, registry):
        self.registry = registry
        self.db = registry.db
        self.events = registry.events
        self.config = registry.config
        self.data_dir = registry.data_dir
        self.root = registry.root

    # -- configuration / reads -------------------------------------------

    @property
    def enabled(self) -> bool:
        if not bool(self.config.get("modules.enabled", True)):
            return False
        return bool(self.config.get("skills.enabled", True))

    def max_body_chars(self) -> int:
        return max(1, int(self.config.get("skills.max_body_chars", 6000)))

    def all(self) -> list[Module]:
        return [module for module in self.registry.all() if module.kind == "skill"]

    def get(self, module_id: str) -> Module | None:
        module = self.registry.get(module_id)
        return module if module and module.kind == "skill" else None

    def find_by_name(self, name: str) -> Module | None:
        wanted = str(name or "").strip().lower()
        if not wanted:
            return None
        for module in self.all():
            if module.name.strip().lower() == wanted:
                return module
        return None

    def is_agent_owned(self, module: Module | None) -> bool:
        """True only for skills the agent itself created.

        Imported and user-created skills are deliberately excluded, even if
        the user later revised them. Only ``create_draft`` by the model or
        builder establishes agent ownership.
        """
        return bool(
            module is not None
            and module.kind == "skill"
            and module.origin == "agent"
            and module.created_by in AGENT_SKILL_CREATORS
        )

    @staticmethod
    def ownership_error(module: Module) -> str:
        owner = module.created_by or module.origin or "another owner"
        return (
            f"skill {module.name!r} was created by {owner}; the agent may "
            "only edit skills it created, not imported or user-created skills"
        )

    def body(self, module: Module) -> str | None:
        """Read a skill body. Missing files are reported, never invented."""
        if not module.content_path:
            return None
        path = Path(module.content_path)
        if not path.is_absolute():
            path = self.data_dir / path
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def body_for(self, module_id: str) -> str | None:
        module = self.get(module_id)
        return self.body(module) if module else None

    def describe(self, module: Module) -> dict[str, Any]:
        """JSON-safe view for the API/CLI (list view keeps a bounded body)."""
        meta = module.manifest or {}
        body = self.body(module) or ""
        return {
            "id": module.id,
            "kind": module.kind,
            "name": module.name,
            "version": module.version,
            "status": module.status,
            "scope": module.scope,
            "description": str(meta.get("description") or ""),
            "preconditions": _as_list(meta.get("preconditions")),
            "domains": _as_list(meta.get("domains")),
            "origin": module.origin,
            "provenance": module.provenance,
            "created_by": module.created_by,
            "agent_owned": self.is_agent_owned(module),
            "created_at": module.created_at,
            "updated_at": module.updated_at,
            "content_path": module.content_path,
            "usage_count": module.usage_count,
            "body_chars": len(body),
            "body_preview": util.clip(body, 500),
            "revisions": list(meta.get("revisions") or [])[-AUDIT_LIMIT:],
            "rollback_available": bool(meta.get("version_history")),
        }

    def list_skills(self) -> list[dict[str, Any]]:
        return [self.describe(module) for module in self.all()]

    # -- validation ------------------------------------------------------

    def validate_fields(
        self,
        *,
        name: str,
        description: str,
        preconditions: Any = None,
        domains: Any = None,
        body: str,
    ) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise ValueError("skill name must not be empty")
        if len(name) > MAX_NAME_CHARS:
            raise ValueError(
                f"skill name is {len(name)} chars; limit is {MAX_NAME_CHARS}"
            )
        if any(char in name for char in "\r\n\t"):
            raise ValueError("skill name must be one line")
        description = str(description or "").strip()
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise ValueError(
                f"skill description is {len(description)} chars; limit is "
                f"{MAX_DESCRIPTION_CHARS}"
            )
        pre = _as_list(preconditions)
        if len(pre) > MAX_PRECONDITIONS:
            raise ValueError(
                f"a skill may have at most {MAX_PRECONDITIONS} preconditions"
            )
        for token in pre:
            if len(token) > MAX_PRECONDITION_CHARS:
                raise ValueError(
                    f"precondition {token[:30]!r} is longer than "
                    f"{MAX_PRECONDITION_CHARS} characters"
                )
        doms = _as_list(domains)
        if len(doms) > MAX_DOMAINS:
            raise ValueError(
                f"a skill may be bound to at most {MAX_DOMAINS} domains"
            )
        body = str(body or "").strip()
        if not body:
            raise ValueError("skill body must not be empty")
        limit = self.max_body_chars()
        if len(body) > limit:
            raise ValueError(
                f"skill body is {len(body)} chars; limit is {limit} "
                "(skills.max_body_chars)"
            )
        if "\x00" in body:
            raise ValueError("skill body must not contain NUL bytes")
        return {
            "name": name,
            "description": description,
            "preconditions": pre,
            "domains": doms,
            "body": body,
        }

    # -- import ----------------------------------------------------------

    def import_configured(self) -> list[str]:
        """Import ``tools.skills_dir`` rows that do not exist yet."""
        if not self.enabled:
            return []
        path_text = str(self.config.get("tools.skills_dir", "") or "")
        if not path_text:
            return []
        path = Path(path_text)
        if not path.is_absolute():
            path = self.data_dir / path
        if not path.is_dir():
            self.events.record(
                "skill.config_missing", level="warn",
                data={"path": str(path)},
            )
            return []
        return self.import_dir(path)

    def import_dir(self, path: Path) -> list[str]:
        imported: list[str] = []
        for skill_dir in sorted(Path(path).iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                text = skill_file.read_text(encoding="utf-8")
            except OSError as exc:
                self.events.record(
                    "skill.import_failed", level="warn",
                    data={"path": str(skill_file),
                          "error": f"{type(exc).__name__}: {exc}"},
                )
                continue
            meta, body = parse_front_matter(text)
            name = str(meta.get("name") or skill_dir.name).strip()
            try:
                fields = self.validate_fields(
                    name=name,
                    description=str(meta.get("description") or ""),
                    preconditions=meta.get("preconditions"),
                    domains=meta.get("domains"),
                    body=body,
                )
                module_id = self._resolve_id(fields["name"])
            except ValueError as exc:
                self.events.record(
                    "skill.import_failed", level="warn",
                    data={"path": str(skill_file), "error": str(exc)},
                )
                continue
            # The directory stays an import source; an existing row is
            # authoritative and is never overwritten by a later load.
            if self.registry.get(module_id) is not None or self.find_by_name(
                fields["name"]
            ) is not None:
                continue
            domains = fields["domains"]
            preconditions = fields["preconditions"]
            scope = (
                "domain" if domains
                else "adaptive" if preconditions
                else "global"
            )
            content_path = self._write_body(module_id, 1, fields["body"])
            now = util.iso()
            module = Module(
                id=module_id,
                kind="skill",
                name=fields["name"],
                version=1,
                status="active",
                scope=scope,
                manifest={
                    "description": fields["description"],
                    "preconditions": preconditions,
                    "domains": domains,
                    "source": str(skill_file),
                    "revisions": [{
                        "version": 1, "status": "active",
                        "changed_at": now, "changed_by": "import",
                        "reason": "imported from SKILL.md",
                    }],
                    "version_history": [],
                },
                content_path=content_path,
                origin="import",
                provenance={"source": str(skill_file)},
                created_by="import",
                created_at=now,
                updated_at=now,
            )
            self.registry.save(module)
            self._set_bindings(module_id, domains)
            imported.append(module_id)
            self.events.record(
                "module.imported",
                data={"module_id": module_id, "kind": "skill",
                      "source": str(skill_file)},
            )
        return imported

    # -- disclosure ------------------------------------------------------

    def disclose(
        self, domain_id: str | None, objective: str
    ) -> tuple[str, list[dict[str, Any]]]:
        """Return the disclosed skill block plus per-skill source records."""
        sources: list[dict[str, Any]] = []
        if not self.enabled:
            return "", sources
        modules = self.all()
        if not modules:
            return "", sources
        words = set(util.keywords(objective))
        objective_lower = (objective or "").lower()
        blocks: list[str] = []
        for module in modules:
            meta = module.manifest or {}
            preconditions = _as_list(meta.get("preconditions"))
            if module.status != "active":
                sources.append({
                    "section": "skills", "ref": module.id,
                    "reason": f"not applicable: module is {module.status}",
                })
                continue
            if module.scope == "domain":
                bound = {
                    binding.domain_id
                    for binding in self.registry.bindings(module.id)
                    if binding.enabled
                }
                if not domain_id or domain_id not in bound:
                    sources.append({
                        "section": "skills", "ref": module.id,
                        "reason": (
                            "not applicable: no enabled binding for this "
                            "domain"
                        ),
                    })
                    continue
                if preconditions and not self._matches(
                    preconditions, words, objective_lower
                ):
                    sources.append({
                        "section": "skills", "ref": module.id,
                        "reason": "not applicable: preconditions not in "
                                  "objective",
                    })
                    continue
            elif module.scope == "adaptive":
                if not preconditions or not self._matches(
                    preconditions, words, objective_lower
                ):
                    sources.append({
                        "section": "skills", "ref": module.id,
                        "reason": "not applicable: preconditions not in "
                                  "objective",
                    })
                    continue
            elif module.scope != "global":
                sources.append({
                    "section": "skills", "ref": module.id,
                    "reason": f"not applicable: unknown scope {module.scope}",
                })
                continue
            body = self.body(module)
            if body is None:
                sources.append({
                    "section": "skills", "ref": module.id,
                    "reason": "not applicable: skill body file is missing",
                })
                continue
            blocks.append(
                f"## Skill: {module.name}\n"
                f"{util.clip(body, DISCLOSURE_BODY_CHARS)}"
            )
            if module.scope == "domain":
                reason = "domain-bound skill for this domain"
            elif module.scope == "adaptive":
                reason = "skill preconditions matched the objective"
            else:
                reason = "global skill"
            sources.append({
                "section": "skills", "ref": module.id, "reason": reason,
            })
        if not blocks:
            return "", sources
        return (
            "# Applicable skills (instructions; effects still require tools "
            "and permissions)\n" + "\n".join(blocks)
        ), sources

    @staticmethod
    def _matches(
        preconditions: list[str], words: set[str], objective_lower: str
    ) -> bool:
        for token in preconditions:
            cleaned = token.strip().lower()
            if not cleaned:
                continue
            if cleaned in words:
                return True
            if len(cleaned) > 2 and cleaned in objective_lower:
                return True
        return False

    # -- lifecycle -------------------------------------------------------

    def create_draft(
        self,
        *,
        name: str,
        description: str,
        preconditions: Any = None,
        domains: Any = None,
        body: str,
        origin: str = "agent",
        provenance: dict[str, Any] | None = None,
        created_by: str = "model",
        reason: str | None = None,
    ) -> Module:
        """Create or revise one skill row as ``status=draft``.

        Re-proposing the same name revises the same row, writes a new body
        version, and records the previous state for rollback. It never
        activates anything.
        """
        if not self.enabled:
            raise ValueError("skills are disabled in configuration")
        fields = self.validate_fields(
            name=name, description=description,
            preconditions=preconditions, domains=domains, body=body,
        )
        module_id = self._resolve_id(fields["name"])
        existing = self.registry.get(module_id)
        if existing is not None and existing.kind != "skill":
            raise ValueError(f"module id {module_id} is not a skill")
        if existing is not None and existing.name.strip().lower() != (
            fields["name"].strip().lower()
        ):
            raise ValueError(
                f"module id {module_id} already belongs to "
                f"{existing.name!r}"
            )
        if (
            existing is not None
            and created_by in AGENT_SKILL_CREATORS
            and not self.is_agent_owned(existing)
        ):
            # The agent may refine its own procedures, never imported or
            # user-authored ones. The UI passes created_by="user" and keeps
            # the explicit user override.
            raise ValueError(self.ownership_error(existing))
        manifest = dict(existing.manifest) if existing else {}
        history = list(manifest.get("version_history") or [])
        if existing is not None:
            history.append(self._snapshot(
                existing,
                reason=reason or "revised",
                changed_by=created_by,
            ))
            history = history[-HISTORY_LIMIT:]
        next_version = self._next_version(existing, manifest)
        content_path = self._write_body(
            module_id, next_version, fields["body"]
        )
        now = util.iso()
        domains_list = fields["domains"]
        preconditions_list = fields["preconditions"]
        scope = (
            "domain" if domains_list
            else "adaptive" if preconditions_list
            else "global"
        )
        manifest.update({
            "description": fields["description"],
            "preconditions": preconditions_list,
            "domains": domains_list,
            "version_history": history,
        })
        revisions = list(manifest.get("revisions") or [])
        revisions.append({
            "version": next_version, "status": "draft",
            "changed_at": now, "changed_by": created_by,
            "reason": reason or ("revised" if existing else "created"),
        })
        manifest["revisions"] = revisions[-AUDIT_LIMIT:]
        module = Module(
            id=module_id,
            kind="skill",
            name=fields["name"],
            version=next_version,
            status="draft",
            scope=scope,
            manifest=manifest,
            content_path=content_path,
            origin=existing.origin if existing else origin,
            provenance=(
                dict(existing.provenance) if existing
                else dict(provenance or {})
            ),
            created_by=existing.created_by if existing else created_by,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self.registry.save(module)
        self._set_bindings(module_id, domains_list)
        self.events.record(
            "skill.draft_proposed", source=created_by,
            data={
                "module_id": module_id,
                "name": module.name,
                "version": next_version,
                "revised": existing is not None,
                "origin": module.origin,
                "provenance": module.provenance,
            },
        )
        return self.registry.get(module_id) or module

    def edit_plan(
        self,
        *,
        name: str,
        description: str | None = None,
        preconditions: Any = None,
        domains: Any = None,
        body: str | None = None,
    ) -> tuple[Module, dict[str, Any]]:
        """Resolve an agent-owned skill and merge the fields to be revised.

        Omitted fields keep their current values. Validation happens here, so
        the runtime can reject a bad edit before asking the user to approve
        it.
        """
        module = self.find_by_name(name)
        if module is None:
            raise ValueError(f"no skill named {name!r}")
        if not self.is_agent_owned(module):
            raise ValueError(self.ownership_error(module))
        manifest = module.manifest or {}
        current_body = self.body(module)
        if current_body is None:
            raise ValueError(
                f"skill {module.id} has no readable body and cannot be edited"
            )
        fields = self.validate_fields(
            name=module.name,
            description=(
                str(manifest.get("description") or "")
                if description is None else description
            ),
            preconditions=(
                manifest.get("preconditions")
                if preconditions is None else preconditions
            ),
            domains=(
                manifest.get("domains")
                if domains is None else domains
            ),
            body=current_body if body is None else body,
        )
        return module, fields

    def edit(
        self,
        *,
        name: str,
        description: str | None = None,
        preconditions: Any = None,
        domains: Any = None,
        body: str | None = None,
        created_by: str = "model",
        provenance: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> Module:
        """Revise an agent-created skill as a new draft version."""
        module, fields = self.edit_plan(
            name=name, description=description,
            preconditions=preconditions, domains=domains, body=body,
        )
        return self.create_draft(
            name=fields["name"],
            description=fields["description"],
            preconditions=fields["preconditions"],
            domains=fields["domains"],
            body=fields["body"],
            origin=module.origin,
            provenance=provenance,
            created_by=created_by,
            reason=reason or "refined by the agent",
        )

    def activate(self, module_id: str) -> Module:
        module = self._require(module_id)
        body = self.body(module)
        if body is None:
            raise ValueError(
                f"skill {module_id} has no readable body and cannot be "
                "activated"
            )
        fields = self.validate_fields(
            name=module.name,
            description=str((module.manifest or {}).get("description") or ""),
            preconditions=(module.manifest or {}).get("preconditions"),
            domains=(module.manifest or {}).get("domains"),
            body=body,
        )
        if module.status == "active":
            return module
        updated = replace(module, status="active", updated_at=util.iso())
        self.registry.save(updated)
        self._audit(updated, reason="activated", changed_by="user")
        self.events.record(
            "skill.activated", data={
                "module_id": module.id, "version": updated.version,
                "scope": updated.scope,
                "preconditions": fields["preconditions"],
            },
        )
        return self.registry.get(module_id) or updated

    def disable(self, module_id: str) -> Module:
        module = self._require(module_id)
        if module.status == "disabled":
            return module
        updated = replace(module, status="disabled", updated_at=util.iso())
        self.registry.save(updated)
        self._audit(updated, reason="disabled", changed_by="user")
        self.events.record(
            "skill.disabled", data={"module_id": module.id,
                                    "version": updated.version},
        )
        return self.registry.get(module_id) or updated

    def set_enabled(self, module_id: str, enabled: bool) -> Module:
        return self.activate(module_id) if enabled else self.disable(module_id)

    def rollback(self, module_id: str) -> Module:
        """Restore the previous versioned state (one-click rollback)."""
        module = self._require(module_id)
        manifest = dict(module.manifest or {})
        history = list(manifest.get("version_history") or [])
        now = util.iso()
        if history:
            snapshot = history.pop()
            content_path = snapshot.get("content_path")
            if not content_path or not self._path_exists(content_path):
                raise ValueError(
                    f"rollback target for {module_id} is missing: "
                    f"{content_path}"
                )
            version = int(snapshot.get("version") or 1)
            status = str(snapshot.get("status") or "active")
            if status not in ("draft", "active", "disabled", "error"):
                status = "active"
            for key in ("description", "preconditions", "domains"):
                if key in snapshot:
                    manifest[key] = snapshot[key]
            manifest["version_history"] = history
            manifest["revisions"] = list(manifest.get("revisions") or [])
            manifest["revisions"].append({
                "version": version, "status": status,
                "changed_at": now, "changed_by": "user",
                "reason": f"rolled back from v{module.version}",
            })
            manifest["revisions"] = manifest["revisions"][-AUDIT_LIMIT:]
            updated = replace(
                module,
                version=version,
                status=status,
                scope=str(snapshot.get("scope") or module.scope),
                manifest=manifest,
                content_path=str(content_path),
                updated_at=now,
            )
            self.registry.save(updated)
            self._set_bindings(module_id, snapshot.get("bindings") or [])
            self.events.record(
                "skill.rolled_back", data={
                    "module_id": module_id,
                    "from_version": module.version,
                    "to_version": version,
                    "status": status,
                },
            )
            return self.registry.get(module_id) or updated

        # No recorded snapshot: fall back to the previous body file when it
        # exists. Lifecycle state and bindings stay as they are.
        if module.version <= 1:
            raise ValueError(f"skill {module_id} has no previous version")
        previous = module.version - 1
        content_path = self._version_path(module_id, previous)
        if not self._path_exists(content_path):
            raise ValueError(
                f"skill {module_id} has no previous body file (v{previous})"
            )
        revision_entry = {
            "version": previous, "status": module.status,
            "changed_at": now, "changed_by": "user",
            "reason": f"rolled back from v{module.version}",
        }
        revisions = list(manifest.get("revisions") or [])
        revisions.append(revision_entry)
        manifest["revisions"] = revisions[-AUDIT_LIMIT:]
        updated = replace(
            module,
            version=previous,
            manifest=manifest,
            content_path=content_path,
            updated_at=now,
        )
        self.registry.save(updated)
        self.events.record(
            "skill.rolled_back", data={
                "module_id": module_id,
                "from_version": module.version,
                "to_version": previous,
                "status": module.status,
            },
        )
        return self.registry.get(module_id) or updated

    def delete(self, module_id: str) -> dict[str, Any]:
        """Delete a non-active skill row and its versioned body files.

        Active skills must be disabled first: deleting the currently disclosed
        procedure while it is live would make the UI lie about what the agent
        can use. The audit event records the identity and version that went
        away.
        """
        module = self._require(module_id)
        if module.status == "active":
            raise ValueError(
                f"skill {module_id} is active; disable it before deleting"
            )
        self.db.execute(
            "DELETE FROM module_bindings WHERE module_id=?", (module_id,)
        )
        self.db.execute(
            "DELETE FROM module_tools WHERE module_id=?", (module_id,)
        )
        self.db.execute("DELETE FROM modules WHERE id=?", (module_id,))
        shutil.rmtree(self._skill_dir(module_id), ignore_errors=True)
        self.events.record(
            "skill.deleted",
            data={
                "module_id": module_id,
                "name": module.name,
                "version": module.version,
                "status": module.status,
            },
        )
        return {"deleted": module_id, "name": module.name}

    def reload(self, module_id: str) -> dict[str, Any]:
        module = self._require(module_id)
        if self.body(module) is None:
            self.registry.set_last_error(
                module_id, "skill body file is missing"
            )
            self.events.record(
                "module.unavailable", level="warn",
                data={"module_id": module_id,
                      "error": "skill body file is missing"},
            )
            return {
                "module_id": module_id, "loaded": False,
                "status": module.status,
            }
        self.registry.mark_loaded(module_id)
        self.events.record(
            "module.reloaded",
            data={"module_id": module_id, "loaded": True,
                  "kind": "skill"},
        )
        return {
            "module_id": module_id, "loaded": True,
            "status": module.status,
        }

    # -- helpers ---------------------------------------------------------

    def _require(self, module_id: str) -> Module:
        module = self.get(module_id)
        if module is None:
            raise ValueError(f"no such skill: {module_id}")
        return module

    def _resolve_id(self, name: str) -> str:
        existing = self.find_by_name(name)
        if existing is not None:
            return existing.id
        base = skill_module_id(name)
        taken = self.registry.get(base)
        if taken is None or taken.name.strip().lower() == name.strip().lower():
            return base
        for index in range(2, 100):
            candidate = f"{base}-{index}"
            if self.registry.get(candidate) is None:
                return candidate
        raise ValueError(f"cannot allocate a module id for skill {name!r}")

    def _next_version(
        self, existing: Module | None, manifest: dict[str, Any]
    ) -> int:
        highest = existing.version if existing is not None else 0
        for revision in manifest.get("revisions") or []:
            try:
                highest = max(highest, int(revision.get("version") or 0))
            except (TypeError, ValueError):
                continue
        for revision in manifest.get("version_history") or []:
            try:
                highest = max(highest, int(revision.get("version") or 0))
            except (TypeError, ValueError):
                continue
        if existing is not None:
            for path in self._skill_dir(existing.id).glob("v*.md"):
                match = re.fullmatch(r"v(\d+)\.md", path.name)
                if match:
                    highest = max(highest, int(match.group(1)))
        return highest + 1

    def _snapshot(
        self, module: Module, *, reason: str, changed_by: str
    ) -> dict[str, Any]:
        meta = module.manifest or {}
        return {
            "version": module.version,
            "status": module.status,
            "scope": module.scope,
            "content_path": module.content_path,
            "description": str(meta.get("description") or ""),
            "preconditions": _as_list(meta.get("preconditions")),
            "domains": _as_list(meta.get("domains")),
            "bindings": [
                binding.domain_id
                for binding in self.registry.bindings(module.id)
                if binding.enabled
            ],
            "changed_at": util.iso(),
            "changed_by": changed_by,
            "reason": reason,
        }

    def _audit(
        self, module: Module, *, reason: str, changed_by: str
    ) -> None:
        manifest = dict(module.manifest or {})
        revisions = list(manifest.get("revisions") or [])
        revisions.append({
            "version": module.version,
            "status": module.status,
            "changed_at": util.iso(),
            "changed_by": changed_by,
            "reason": reason,
        })
        manifest["revisions"] = revisions[-AUDIT_LIMIT:]
        self.registry.save(replace(
            module, manifest=manifest, updated_at=module.updated_at
        ))

    def _skill_dir(self, module_id: str) -> Path:
        return self.root / "skills" / _safe_dir(module_id)

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.data_dir).as_posix()
        except ValueError:
            return path.as_posix()

    def _version_path(self, module_id: str, version: int) -> str:
        return self._relative(self._skill_dir(module_id) / f"v{version}.md")

    def _path_exists(self, content_path: str) -> bool:
        path = Path(content_path)
        if not path.is_absolute():
            path = self.data_dir / path
        return path.is_file()

    def _write_body(
        self, module_id: str, version: int, body: str
    ) -> str:
        path = self._skill_dir(module_id) / f"v{version}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(body).strip() + "\n", encoding="utf-8")
        return self._relative(path)

    def _set_bindings(self, module_id: str, domains: list[str]) -> None:
        wanted = set(domains)
        for domain_id in wanted:
            self.registry.set_binding(ModuleBinding(
                module_id=module_id, domain_id=domain_id, enabled=True,
            ))
        for binding in self.registry.bindings(module_id):
            if binding.domain_id not in wanted and binding.enabled:
                self.registry.set_binding(ModuleBinding(
                    module_id=module_id,
                    domain_id=binding.domain_id,
                    enabled=False,
                    risk_override=binding.risk_override,
                ))


class SkillBuilder:
    """Bounded heartbeat pass that may propose at most one draft skill.

    It reads only successfully completed, tool-using tasks; tainted runs are
    refused and never used. It writes exactly one optional draft row, a
    markdown log under ``artifacts/skills/``, and an event. Any parse,
    validation, or model failure changes no skill.
    """

    PROMPT = """You maintain the reusable procedural skills of a personal assistant.

Below is a batch of successfully completed tasks and the tools they used.
Propose at most ONE reusable skill as strict JSON, or none.

Rules:
- A skill is instruction text only: no code, no handlers, no tool schemas.
- It must be a general procedure likely to help on a future similar task.
- Do not include secrets, personal data, URLs, or one-off details.
- Keep the body under {max_body} characters and the name under 80 characters.
- Preconditions are short keywords that indicate when the skill applies.
- Domains bind the skill to existing domain ids; use [] for a global skill.
- To refine one of your own skills, use its exact existing name.
- Never reuse the name of an imported or user-created skill.
- If the batch shows no reusable procedure, answer with {{"skill": null}}.
- Reply with ONLY a JSON object, no prose, no code fence.

Existing agent-created skills you may refine:
{owned_skills}

Completed tasks:
{tasks}

Reply shape:
{{"skill": {{"name": "short name", "description": "one line",
  "preconditions": ["keyword"], "domains": [], "body": "instructions"}}}}
or {{"skill": null}}
"""

    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.config = app.config
        self.events = app.events
        self.model = app.model
        self.tasks = app.tasks
        self.artifacts_dir = app.artifacts_dir
        self.skills: SkillManager = app.modules.skills

    def maybe_draft(self, now=None) -> dict[str, Any]:
        report: dict[str, Any] = {
            "status": "idle", "run_ids": [], "skill_id": None,
            "log_path": None,
        }
        if not self.skills.enabled:
            report["status"] = "disabled"
            return report
        if not bool(self.config.get("skills.auto_draft", True)):
            report["status"] = "disabled"
            return report
        threshold = max(
            1, int(self.config.get("skills.draft_after_successful_tasks", 5))
        )
        max_per_day = max(
            0, int(self.config.get("skills.max_drafts_per_day", 2))
        )
        if max_per_day and self._drafts_today(now) >= max_per_day:
            report["status"] = "daily_limit"
            return report

        seen = self._seen_runs()
        unseen = [
            run for run in self._successful_runs()
            if str(run["id"]) not in seen
        ]
        if not unseen:
            return report

        tainted: list[dict[str, Any]] = []
        trusted: list[dict[str, Any]] = []
        for run in unseen:
            meta = util.loads(run.get("meta") or "", {})
            sources = (
                meta.get("tainted_sources")
                if isinstance(meta, dict)
                else None
            )
            (tainted if sources else trusted).append(run)
        # Tainted provenance is refused explicitly and never used.
        if tainted:
            tainted_ids = [str(run["id"]) for run in tainted]
            self.events.record(
                "skill.refused", level="warn", source="builder",
                data={
                    "reason": "untrusted provenance",
                    "run_ids": tainted_ids,
                    "origins": [
                        str(entry.get("origin"))
                        for run in tainted
                        for entry in (
                            util.loads(run.get("meta") or "", {}).get(
                                "tainted_sources"
                            ) or []
                        )
                    ][:5],
                },
            )
            seen |= set(tainted_ids)
            self._save_seen(seen)
        report["refused_runs"] = [
            str(run["id"]) for run in tainted
        ]

        if len(trusted) < threshold:
            report["status"] = "waiting"
            report["pending_runs"] = len(trusted)
            return report

        batch = trusted[:threshold]
        run_ids = [str(run["id"]) for run in batch]
        report["run_ids"] = run_ids
        prompt = self.PROMPT.format(
            max_body=self.skills.max_body_chars(),
            owned_skills=self._owned_skill_index(),
            tasks=self._task_summaries(batch),
        )
        try:
            reply = self.model.chat(
                [{"role": "user", "content": prompt}],
                tools=[],
                max_tokens=1200,
                thinking=False,
            )
        except (ModelUnavailable, ModelResponseError) as exc:
            report["status"] = "failed"
            report["error"] = f"{type(exc).__name__}: {exc}"
        else:
            skill, error = self._parse(reply.content)
            if error is not None:
                report["status"] = "failed"
                report["error"] = error
            elif skill is None:
                report["status"] = "no_proposal"
            else:
                report["status"] = "proposed"
                try:
                    module = self.skills.create_draft(
                        name=str(skill.get("name") or ""),
                        description=str(skill.get("description") or ""),
                        preconditions=skill.get("preconditions"),
                        domains=skill.get("domains"),
                        body=str(skill.get("body") or ""),
                        origin="agent",
                        provenance={
                            "source": "skill_builder",
                            "source_runs": run_ids,
                        },
                        created_by="builder",
                        reason=(
                            f"auto-draft after {len(batch)} successful "
                            "tool-using tasks"
                        ),
                    )
                    report["status"] = "created"
                    report["skill_id"] = module.id
                except ValueError as exc:
                    report["status"] = "rejected"
                    report["error"] = str(exc)

        seen |= set(run_ids)
        self._save_seen(seen)
        report["log_path"] = self._write_log(report, prompt)
        self.db.meta_set("skills.last_builder_at", util.iso(now))
        self.events.record(
            "skill.builder", source="builder",
            level="info" if report["status"] in (
                "created", "no_proposal"
            ) else "warn",
            data={
                "status": report["status"],
                "run_ids": run_ids,
                "skill_id": report.get("skill_id"),
                "log_path": report.get("log_path"),
                "error": report.get("error"),
            },
        )
        return report

    # -- builder helpers -------------------------------------------------

    def _successful_runs(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT r.* FROM runs r JOIN ("
            " SELECT task_id, MAX(started_at) AS newest FROM runs"
            " WHERE status='completed' AND tool_calls>0 GROUP BY task_id"
            ") latest ON latest.task_id=r.task_id"
            " AND latest.newest=r.started_at"
            " ORDER BY r.started_at"
        )
        return [dict(row) for row in rows]

    def _task_summaries(self, runs: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for run in runs:
            task = self.tasks.get(str(run["task_id"]))
            objective = task["objective"] if task else "(task removed)"
            executions = self.db.query(
                "SELECT tool, status FROM tool_executions WHERE run_id=?"
                " ORDER BY started_at", (run["id"],),
            )
            tools = ", ".join(
                f"{row['tool']}({row['status']})" for row in executions
            ) or "no recorded tool names"
            lines.append(
                f"- objective: {util.clip(objective, 300)}\n"
                f"  tools: {util.clip(tools, 300)}"
            )
        return "\n".join(lines) or "(none)"

    def _owned_skill_index(self) -> str:
        lines: list[str] = []
        for module in self.skills.all():
            if not self.skills.is_agent_owned(module):
                continue
            description = str(
                (module.manifest or {}).get("description") or ""
            )
            lines.append(
                f"- {module.name} (v{module.version}, {module.status}): "
                f"{util.clip(description, 160) or '(no description)'}"
            )
        return "\n".join(lines[:20]) or "(none)"

    @staticmethod
    def _parse(content: str) -> tuple[dict[str, Any] | None, str | None]:
        text = (content or "").strip()
        if not text:
            return None, "empty model reply"
        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            return None, f"reply is not strict JSON: {exc}"
        if not isinstance(data, dict):
            return None, "JSON reply is not an object"
        if "skill" not in data:
            return None, "JSON reply has no 'skill' key"
        skill = data["skill"]
        if skill is None:
            return None, None
        if not isinstance(skill, dict):
            return None, "'skill' must be an object or null"
        return skill, None

    def _seen_runs(self) -> set[str]:
        raw = self.db.meta_get("skills.seen_runs") or "[]"
        data = util.loads(raw, [])
        return {str(item) for item in data} if isinstance(data, list) else set()

    def _save_seen(self, seen: set[str]) -> None:
        ordered = sorted(seen)[-1000:]
        self.db.meta_set("skills.seen_runs", util.dumps(ordered))

    def _drafts_today(self, now=None) -> int:
        now = now or util.now_utc()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        row = self.db.query_one(
            "SELECT COUNT(*) AS c FROM event_log WHERE"
            " type='skill.draft_proposed' AND source='builder' AND ts>=?",
            (util.iso(start),),
        )
        return int(row["c"]) if row else 0

    def _write_log(
        self, report: dict[str, Any], prompt: str
    ) -> str:
        run_id = util.new_id("skl")
        rel = f"skills/{run_id}.md"
        path = self.artifacts_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Skill builder run {run_id}",
            f"- status: {report.get('status')}",
            f"- source runs: {', '.join(report.get('run_ids') or []) or '-'}",
            f"- refused runs: "
            f"{', '.join(report.get('refused_runs') or []) or '-'}",
            f"- skill id: {report.get('skill_id') or '-'}",
            f"- error: {report.get('error') or '-'}",
            "",
            "## Prompt",
            prompt,
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return rel

"""Memory consolidation: bounded, inspectable, revertible.

Consolidation reviews accumulated messages and active memories, proposes
changes (create, update, merge duplicates, resolve contradictions, expire
stale information), and applies them through the memory manager so every
change carries provenance (``consolidation:<run_id>``), a revision, and a
line in a markdown change log under ``artifacts/consolidation/``. It runs
manually (CLI/UI) and optionally on a schedule. It is never a prerequisite
for ordinary memory capture or task resumption, and a failure changes
nothing.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import util
from .db import Database
from .model import Model, ModelResponseError, ModelUnavailable
from .store import Events

# Joins models use to staple separate facts into one memory; consolidation
# refuses content that contains them so a single row never becomes a chain of
# restatements.
_STITCHED = re.compile(
    r"(?:^|[\s.;])(?:also|additionally|moreover|furthermore|in addition)"
    r"\s*[:,]",
    re.IGNORECASE,
)

_PROMPT = """You are consolidating the memory of a personal assistant.

Below are the assistant's active memories and a transcript of recent
conversation. Propose a small set of memory changes as strict JSON.

Allowed operations:
- create: one durable fact/preference/decision worth keeping that is not stored yet
- update: correct or refine one existing memory (memory_id + new content + reason)
- merge: two memories say the same thing (source_id folds into target_id); optional content replaces the target text with one clearer statement
- retract: a memory is wrong, obsolete, or worthless (memory_id + reason)

Rules:
- One memory is one short, self-contained statement. Keep separate facts in
  separate memories; never bundle several facts into one entry.
- Never prefix content with "User:" and never stitch statements together with
  "Also:", "Additionally:", "Moreover:", or similar joins.
- Only durable information: no transient chatter, no restating what another
  memory already covers.
- Resolve contradictions in favor of the newer statement in the transcript.
- Merge only memories that state the same subject and fact. Omit content when
  the target already says it best; otherwise give one concise statement that
  absorbs the source. Never copy both texts into content.
- At most {max_changes} changes. Empty list if nothing is worth changing.
- Reply with ONLY a JSON object: {{"changes": [{{"op": "...", ...}}]}}

Active memories:
{memories}

Recent conversation:
{transcript}
"""


class Consolidation:
    def __init__(self, app):
        self.app = app
        self.db: Database = app.db
        self.events: Events = app.events
        self.memory = app.memory
        self.model: Model = app.model
        self.messages = app.messages
        self.config = app.config
        self.artifacts_dir = app.artifacts_dir

    def run(self, trigger: str = "manual") -> dict[str, Any]:
        run_id = util.new_id("con")
        report: dict[str, Any] = {
            "run_id": run_id, "trigger": trigger, "status": "failed",
            "applied": [], "rejected": [], "log_path": None,
        }
        max_memories = int(self.config.get("consolidation.max_memories", 60))
        max_messages = int(self.config.get("consolidation.max_messages", 60))
        max_changes = int(self.config.get("consolidation.max_changes", 20))

        memories = self.memory.list(limit=max_memories)
        recent = self.messages.recent(limit=max_messages, include_hidden=False)
        if not memories and not recent:
            report["status"] = "nothing_to_do"
            self._finish(run_id, report)
            return report

        prompt = _PROMPT.format(
            max_changes=max_changes,
            memories="\n".join(
                f"- {m['id']} [{m['kind']}, {m['scope']}]: {m['content']}"
                for m in memories
            ) or "(none)",
            transcript="\n".join(
                f"{m['role']}: {util.clip(m['content'], 300)}"
                for m in recent
            ) or "(none)",
        )
        try:
            reply = self.model.chat(
                [{"role": "user", "content": prompt}], tools=[],
                max_tokens=2048,
            )
        except (ModelUnavailable, ModelResponseError) as exc:
            report["error"] = f"model unavailable: {exc}"
            self._finish(run_id, report)
            return report

        changes = self._parse_changes(reply.content, max_changes)
        changed_by = f"consolidation:{run_id}"
        for change in changes:
            outcome = self._apply(change, changed_by)
            bucket = report["applied"] if outcome.get("ok") else report["rejected"]
            bucket.append(
                {"change": change, "result": outcome.get("detail")}
            )
        report["status"] = "completed" if changes else "no_changes"
        report["log_path"] = self._write_log(run_id, report)
        self.db.meta_set("consolidation.last_run", util.iso())
        self._finish(run_id, report)
        return report

    # -- helpers -----------------------------------------------------------

    def _parse_changes(self, content: str, cap: int) -> list[dict[str, Any]]:
        try:
            start = content.find("{")
            end = content.rfind("}")
            data = json.loads(content[start : end + 1])
        except (ValueError, TypeError):
            return []
        changes = data.get("changes")
        if not isinstance(changes, list):
            return []
        out: list[dict[str, Any]] = []
        for change in changes[:cap]:
            if isinstance(change, dict) and change.get("op") in (
                "create", "update", "merge", "retract"
            ):
                out.append(change)
        return out

    def _apply(self, change: dict[str, Any], changed_by: str) -> dict[str, Any]:
        op = change.get("op")
        try:
            if op == "create":
                content = str(change.get("content", "")).strip()
                return self._apply_create(change, content, changed_by)
            if op == "update":
                bad = self._stitched_content(change.get("content"))
                if bad:
                    return {"ok": False, "detail": bad}
                self.memory.update(
                    str(change["memory_id"]), content=change.get("content"),
                    changed_by=changed_by, reason=change.get("reason"),
                )
                return {"ok": True, "detail": f"updated {change['memory_id']}"}
            if op == "merge":
                bad = self._stitched_content(change.get("content"))
                if bad:
                    return {"ok": False, "detail": bad}
                self.memory.merge(
                    str(change["source_id"]), str(change["target_id"]),
                    content=change.get("content"), changed_by=changed_by,
                )
                return {"ok": True,
                        "detail": f"merged {change['source_id']} into {change['target_id']}"}
            if op == "retract":
                self.memory.retract(
                    str(change["memory_id"]), changed_by=changed_by,
                    reason=change.get("reason"),
                )
                return {"ok": True, "detail": f"retracted {change['memory_id']}"}
        except Exception as exc:
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        return {"ok": False, "detail": f"unknown op {op}"}

    def _apply_create(
        self, change: dict[str, Any], content: str, changed_by: str
    ) -> dict[str, Any]:
        bad = self._stitched_content(content)
        if bad:
            return {"ok": False, "detail": bad}
        result = self.memory.remember(
            content,
            kind=change.get("kind"),
            scope=change.get("scope"),
            origin="consolidation",
            provenance={"changed_by": changed_by},
            domains=self.app.domains,
        )
        return {"ok": True, "detail": f"created {result['memory']['id']}"}

    @staticmethod
    def _stitched_content(content: Any) -> str | None:
        """Reject memories that glue separate facts together.

        The model used to answer with entries like ``User is 24. Also: User
        likes pizza.``; one memory must hold one statement, so such a change
        is refused and reported in the run log instead of being stored.
        """
        if not content or not isinstance(content, str):
            return None
        if _STITCHED.search(content):
            return ("content joins several facts with 'Also:'-style markers;"
                    " store one statement per memory")
        return None

    def _write_log(self, run_id: str, report: dict[str, Any]) -> str:
        rel = f"consolidation/{run_id}.md"
        path = self.artifacts_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Consolidation run {run_id}",
            f"- trigger: {report['trigger']}",
            f"- status: {report['status']}",
            f"- applied: {len(report['applied'])}",
            f"- rejected: {len(report['rejected'])}",
            "",
            "## Applied changes",
        ]
        for item in report["applied"]:
            lines.append(f"- {util.dumps(item['change'])} -> {item['result']}")
        lines.append("")
        lines.append("## Rejected changes")
        for item in report["rejected"]:
            lines.append(f"- {util.dumps(item['change'])} -> {item['result']}")
        path.write_text("\n".join(lines), encoding="utf-8")
        return rel

    def _finish(self, run_id: str, report: dict[str, Any]) -> None:
        self.events.record(
            "consolidation.run",
            data={
                "run_id": run_id, "trigger": report["trigger"],
                "status": report["status"],
                "applied": len(report["applied"]),
                "rejected": len(report["rejected"]),
                "log_path": report.get("log_path"),
                "error": report.get("error"),
            },
        )

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        events = self.events.list(limit=limit * 2)
        return [
            util.loads(e["data"] or "{}", {})
            for e in events
            if e["type"] == "consolidation.run"
        ][:limit]

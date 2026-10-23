from __future__ import annotations

from pathlib import Path

from harness.contracts.capabilities import Manifest, OperationSpec


def load_skills(directory: Path) -> list[Manifest]:
    if not directory or not directory.exists():
        return []
    manifests: list[Manifest] = []
    for folder in directory.iterdir():
        skill = folder / "SKILL.md"
        if not skill.is_file():
            continue
        text = skill.read_text(encoding="utf-8")
        name, description, body = _parse_skill(text, folder.name)
        manifests.append(
            Manifest(
                name=name,
                purpose=description,
                source=str(folder),
                operations=[],
                skill_body=body,
            )
        )
    return manifests


def _parse_skill(text: str, fallback: str) -> tuple[str, str, str]:
    if not text.startswith("---"):
        return fallback, fallback, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return fallback, fallback, text
    meta = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip('"')
    return meta.get("name", fallback), meta.get("description", fallback), parts[2].strip()

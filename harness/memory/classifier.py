from __future__ import annotations

from harness.constants import memory_kind_for_type
from harness.contracts.memory import MemoryClassifier
from harness.contracts.settings import SettingsService


def kind_for_type(type_name: str) -> str:
    """Deterministic kind mapping; no model call and no settings."""

    return memory_kind_for_type(type_name)


class KeywordMemoryClassifier:
    """Deterministic memory typing from configurable keyword rules.

    This is deliberately cheap and replaceable: consolidation re-runs it, and
    ``implementations.memory_classifier`` selects another implementation later.
    """

    def __init__(self, settings: SettingsService):
        self._settings = settings

    def classify(self, content: str) -> str:
        text = (content or "").lower()
        for type_name, keywords in self._rules():
            if any(keyword in text for keyword in keywords):
                return type_name
        return str(self._settings.get("memory.classifier.default_type"))

    def kind(self, content: str) -> str:
        return kind_for_type(self.classify(content))

    def _rules(self) -> list[tuple[str, tuple[str, ...]]]:
        raw = str(self._settings.get("memory.classifier.rules") or "")
        rules: list[tuple[str, tuple[str, ...]]] = []
        for chunk in raw.split("|"):
            if ":" not in chunk:
                continue
            type_name, values = chunk.split(":", 1)
            keywords = tuple(item.strip().lower() for item in values.split(",") if item.strip())
            if type_name.strip() and keywords:
                rules.append((type_name.strip(), keywords))
        return rules


class StaticMemoryClassifier:
    """Test/diagnostic classifier that always returns one type."""

    def __init__(self, value: str):
        self._value = value

    def classify(self, content: str) -> str:
        return self._value


__all__ = ["KeywordMemoryClassifier", "StaticMemoryClassifier", "MemoryClassifier", "kind_for_type"]

from __future__ import annotations

from harness.contracts.capabilities import CapabilityExecutor
from harness.contracts.events import EventLog
from harness.contracts.session import SessionManager
from harness.contracts.settings import SettingsService


class CompositeSourceResolver:
    def __init__(
        self,
        sessions: SessionManager,
        events: EventLog,
        capabilities: CapabilityExecutor,
        settings: SettingsService,
    ):
        self._sessions = sessions
        self._events = events
        self._capabilities = capabilities
        self._settings = settings

    def excerpt(self, ref: str) -> str | None:
        if not ref:
            return None
        interaction = self._sessions.get_interaction(ref)
        if interaction is not None:
            parts = [interaction.user_text]
            if interaction.assistant_text:
                parts.append(interaction.assistant_text)
            return "\n".join(parts)
        attempt = self._capabilities.get_attempt(ref)
        if attempt is not None:
            return (
                f"status={attempt.status} error_category={attempt.error_category} "
                f"message={attempt.message} capability={attempt.capability} operation={attempt.operation}"
            )
        for event in self._events.query(limit=int(self._settings.get("memory.source_lookup_events"))):
            if event.id == ref:
                return f"{event.operation} {event.result} {event.data}"
        return None

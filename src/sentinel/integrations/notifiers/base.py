"""Implement this to route Sentinel incidents/notifications to a new
channel. `slack.py` is the one real implementation; `jira.py`/`teams.py` are
worked stubs showing exactly what a real implementation needs — see their
docstrings."""

from __future__ import annotations

from abc import ABC, abstractmethod

from sentinel.core.models import Incident


class NotifierPlugin(ABC):
    name: str  # e.g. "slack", "jira", "teams"

    @abstractmethod
    def notify(self, incident: Incident) -> None:
        """Called by the Incident Automation Engine whenever an incident is
        raised or updated. Must not raise on transient failure — the engine
        catches exceptions defensively, but a well-behaved implementation
        catches and logs its own transport errors so `is_configured()`
        false-negatives are the only reason a notification gets skipped."""
        ...

    @abstractmethod
    def is_configured(self) -> bool:
        """Return False if required env vars/config are missing, so the
        engine can skip this plugin cleanly instead of failing the whole
        notification run."""
        ...

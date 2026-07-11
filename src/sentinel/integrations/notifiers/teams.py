"""Stub Microsoft Teams notifier — always reports unconfigured so the engine
skips it cleanly. `notify()` still logs what it *would* have done, so the
Incident Automation Engine's routing logic is provably correct without real
Teams credentials in CI or the demo environment.

To make this real:
  - Config needed: `TEAMS_WEBHOOK_URL` (an Incoming Webhook connector or
    Workflows webhook URL configured on the target Teams channel).
  - `notify()` would POST an Adaptive Card payload to that URL, roughly:
    `{"type": "message", "attachments": [{"contentType":
    "application/vnd.microsoft.card.adaptive", "content": {"type":
    "AdaptiveCard", "version": "1.4", "body": [...title/severity/link as
    TextBlocks...]}}]}` — Teams deprecated the legacy `MessageCard` format
    in favor of Adaptive Cards, which is the main reason this isn't a
    five-minute add (the card schema has real structure to get right).
  - `is_configured()` would check `TEAMS_WEBHOOK_URL` is non-empty.
"""

from __future__ import annotations

import logging

from sentinel.core.models import Incident
from sentinel.integrations.notifiers.base import NotifierPlugin

logger = logging.getLogger(__name__)


class TeamsNotifier(NotifierPlugin):
    name = "teams"

    def is_configured(self) -> bool:
        return False

    def notify(self, incident: Incident) -> None:
        logger.info(
            "[stub] would post a Teams Adaptive Card for incident %s: %s",
            incident.urn,
            incident.title,
        )

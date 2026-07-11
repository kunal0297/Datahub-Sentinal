"""Real Slack notifier. Posts to an incoming webhook if `SLACK_WEBHOOK_URL`
is set, else via the `chat.postMessage` Bot API if `SLACK_BOT_TOKEN` is set.
Both are stable, long-standing Slack API surfaces — no version-drift risk
comparable to DataHub's tool surface, so no extra verification note here."""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from sentinel.core.config import Settings
from sentinel.core.models import Incident
from sentinel.integrations.notifiers.base import NotifierPlugin

logger = logging.getLogger(__name__)


def _deep_link(frontend_url: str, resource_urn: str) -> str:
    # A guaranteed-correct DataHub route regardless of entity type: per-
    # entity-type frontend paths (e.g. /dataset/<urn> vs plural
    # /mlModels/<urn>) weren't fully verified across versions, but the
    # search page reliably surfaces the exact entity when you search its
    # full URN.
    return f"{frontend_url}/search?query={quote(resource_urn, safe='')}"


def _format_message(incident: Incident, frontend_url: str) -> str:
    body = incident.description.split("<!-- sentinel:dedup_key=")[0].strip()
    lines = [
        f"*{incident.severity.value}* incident on `{', '.join(incident.resource_urns)}`",
        incident.title,
        "",
        body,
    ]
    if incident.owner:
        lines.append(f"Owner: {incident.owner.display_name or incident.owner.urn}")
    lines.append(f"<{_deep_link(frontend_url, incident.resource_urns[0])}|View in DataHub>")
    if incident.link:
        lines.append(f"<{incident.link}|View trigger>")
    return "\n".join(lines)


class SlackNotifier(NotifierPlugin):
    name = "slack"

    def __init__(self, settings: Settings):
        self.settings = settings

    def is_configured(self) -> bool:
        return bool(self.settings.slack_webhook_url or self.settings.slack_bot_token)

    def notify(self, incident: Incident) -> None:
        text = _format_message(incident, self.settings.datahub_frontend_url)
        try:
            if self.settings.slack_webhook_url:
                resp = httpx.post(
                    self.settings.slack_webhook_url, json={"text": text}, timeout=10.0
                )
                resp.raise_for_status()
            elif self.settings.slack_bot_token:
                resp = httpx.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {self.settings.slack_bot_token}"},
                    json={"channel": self.settings.slack_channel, "text": text},
                    timeout=10.0,
                )
                resp.raise_for_status()
                payload = resp.json()
                if not payload.get("ok"):
                    logger.warning("Slack chat.postMessage rejected: %s", payload.get("error"))
        except httpx.HTTPError:
            logger.exception("Slack notify failed for incident %s", incident.urn)

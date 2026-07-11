"""Stub Jira notifier — always reports unconfigured so the engine skips it
cleanly. `notify()` still logs what it *would* have done, so the Incident
Automation Engine's routing logic is provably correct without needing real
Jira credentials in CI or the demo environment.

To make this real:
  - Config needed: `JIRA_BASE_URL`, `JIRA_PROJECT_KEY`, `JIRA_API_TOKEN`,
    `JIRA_EMAIL` (Jira Cloud auth is basic auth with an API token, not a
    bearer token).
  - `notify()` would POST to `{base_url}/rest/api/3/issue` with a payload
    shaped roughly like `{"fields": {"project": {"key": project_key},
    "summary": incident.title, "description": <Atlassian Document Format>,
    "issuetype": {"name": "Bug"}}}` — note Jira Cloud's v3 API wants
    description in ADF, not plain markdown, which is the main reason this
    isn't a five-minute add.
  - `is_configured()` would check all four env vars are non-empty.
"""

from __future__ import annotations

import logging

from sentinel.core.models import Incident
from sentinel.integrations.notifiers.base import NotifierPlugin

logger = logging.getLogger(__name__)


class JiraNotifier(NotifierPlugin):
    name = "jira"

    def is_configured(self) -> bool:
        return False

    def notify(self, incident: Incident) -> None:
        logger.info(
            "[stub] would create a Jira ticket for incident %s: %s", incident.urn, incident.title
        )

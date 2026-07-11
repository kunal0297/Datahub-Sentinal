"""Entrypoint for the packaged GitHub Action
(`.github/actions/pr-impact-analysis`). Reads the standard GitHub Actions
environment (the `pull_request` event payload at `GITHUB_EVENT_PATH`, the
`INPUT_*` variables `action.yml` maps its `with:` block onto) and Sentinel's
own `.env`-driven `Settings`, runs the analysis, posts/updates the PR
comment, and — only if `block_on_critical` is explicitly enabled — exits
non-zero on a CRITICAL finding so the Action's own check fails.

This is the only module in PR Impact Analysis that isn't independently unit
tested end-to-end (it's mostly environment/IO plumbing); `analyzer.py` and
`github_client.py`, which hold all the actual logic, are.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from sentinel.agents.pr_impact.analyzer import analyze_files
from sentinel.agents.pr_impact.github_client import GitHubClient
from sentinel.core.config import Settings, get_settings
from sentinel.core.datahub_client import DataHubClient
from sentinel.core.incident_engine import IncidentEngine, SeverityRules
from sentinel.integrations.notifiers.jira import JiraNotifier
from sentinel.integrations.notifiers.slack import SlackNotifier
from sentinel.integrations.notifiers.teams import TeamsNotifier

logger = logging.getLogger(__name__)

_TRACKED_SUFFIXES = (".sql",)


def _load_event() -> dict:
    event_path = os.environ["GITHUB_EVENT_PATH"]
    return json.loads(Path(event_path).read_text())


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


async def _run(settings: Settings) -> int:
    event = _load_event()
    pr = event["pull_request"]
    pr_number = pr["number"]
    pr_url = pr["html_url"]
    repo = os.environ["GITHUB_REPOSITORY"]
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", "."))

    hop_limit = int(os.environ.get("INPUT_HOP_LIMIT", settings.sentinel_lineage_hop_limit))
    block_on_critical = _env_bool("INPUT_BLOCK_ON_CRITICAL", settings.sentinel_block_on_critical)
    manifest_input = os.environ.get("INPUT_MANIFEST_PATH", "")
    manifest_path = (workspace / manifest_input) if manifest_input else None

    github = GitHubClient(settings.github_token, repo)
    changed_paths = [
        p for p in github.get_changed_files(pr_number) if p.endswith(_TRACKED_SUFFIXES)
    ]
    if not changed_paths:
        logger.info("no tracked (.sql) files changed in PR #%s, nothing to analyze", pr_number)
        return 0

    changed_content = {}
    for rel_path in changed_paths:
        full_path = workspace / rel_path
        if full_path.exists():
            changed_content[full_path] = full_path.read_text()
        else:
            # file was renamed/moved in a way get_changed_files didn't
            # exclude, or checkout didn't include it -- still report it as
            # unresolved rather than crashing the whole run.
            changed_content[full_path] = ""

    severity_rules = SeverityRules.from_yaml(settings.sentinel_severity_rules_path)

    with DataHubClient(settings) as client:
        engine = IncidentEngine(
            client,
            severity_rules,
            notifiers=[SlackNotifier(settings), JiraNotifier(), TeamsNotifier()],
        )
        async with client.mcp():
            result = await analyze_files(
                client,
                engine,
                changed_content,
                severity_rules,
                pr_link=pr_url,
                hop_limit=hop_limit,
                manifest_path=manifest_path,
            )

    github.upsert_pr_comment(pr_number, result.comment_body)
    logger.info(
        "PR Impact Analysis complete for #%s: severity=%s incident=%s",
        pr_number,
        result.overall_severity.value,
        result.incident.urn if result.incident else None,
    )

    if block_on_critical and result.overall_severity.value == "CRITICAL":
        logger.error("blocking: CRITICAL severity and block_on_critical is enabled")
        return 1
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    exit_code = asyncio.run(_run(settings))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

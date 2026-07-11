"""Sentinel's CLI entrypoint. Commands are added as each agent lands —
see ARCHITECTURE.md for the build order. `sentinel --help` always reflects
what's actually implemented; don't add a command stub here before its
agent module exists."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    name="sentinel",
    help="DataHub Sentinel — agents that catch data breakage before it ships.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the installed Sentinel version."""
    from importlib.metadata import version as _version

    try:
        typer.echo(_version("datahub-sentinel"))
    except Exception:
        typer.echo("0.1.0-dev")


@app.command("pr-impact")
def pr_impact(
    repo: Annotated[
        Path,
        typer.Option(help="Path to the repo to analyze (a real PR checkout, or the demo repo)."),
    ] = Path("seed/sample_repo"),
    base_ref: Annotated[
        str,
        typer.Option(
            help="Git ref to diff against — analyzes every .sql file changed since this ref."
        ),
    ] = "HEAD~1",
    pr_link: Annotated[
        str, typer.Option(help="Optional URL embedded in any incident this run raises.")
    ] = "",
    hop_limit: Annotated[
        int | None, typer.Option(help="Downstream lineage hop limit (defaults to Settings).")
    ] = None,
) -> None:
    """Run PR Impact Analysis locally against a real git diff — this is the
    self-contained equivalent of the packaged GitHub Action
    (`.github/actions/pr-impact-analysis`), for demoing without a real PR or
    GitHub token. Prints the same Markdown comment the Action would post."""
    from sentinel.agents.pr_impact.analyzer import analyze_files
    from sentinel.core.config import get_settings
    from sentinel.core.datahub_client import DataHubClient
    from sentinel.core.incident_engine import IncidentEngine, SeverityRules
    from sentinel.integrations.notifiers.jira import JiraNotifier
    from sentinel.integrations.notifiers.slack import SlackNotifier
    from sentinel.integrations.notifiers.teams import TeamsNotifier

    settings = get_settings()
    diff_output = subprocess.run(
        ["git", "diff", "--name-only", base_ref, "--", "*.sql"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    changed_paths = [repo / p for p in diff_output.splitlines() if p.strip()]
    if not changed_paths:
        typer.echo(f"No .sql files changed since {base_ref} in {repo}.")
        raise typer.Exit(0)

    changed_content = {p: p.read_text() for p in changed_paths if p.exists()}
    severity_rules = SeverityRules.from_yaml(settings.sentinel_severity_rules_path)

    async def _run() -> None:
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
                    pr_link=pr_link or None,
                    hop_limit=hop_limit or settings.sentinel_lineage_hop_limit,
                )
        typer.echo(result.comment_body)
        if result.incident:
            typer.echo(f"\nRaised/updated DataHub incident: {result.incident.urn}")

    asyncio.run(_run())


if __name__ == "__main__":
    app()

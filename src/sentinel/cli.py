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


migrate_app = typer.Typer(
    help="Schema Migration Copilot: infer the column mapping, walk lineage, "
    "generate consumer rewrites, and track migration status."
)
app.add_typer(migrate_app, name="migrate")


@migrate_app.callback(invoke_without_command=True)
def migrate(
    ctx: typer.Context,
    from_urn: Annotated[str, typer.Option("--from", help="Old asset URN.")] = "",
    to_urn: Annotated[str, typer.Option("--to", help="New asset URN.")] = "",
    repo: Annotated[Path, typer.Option(help="Target repo containing consumer files.")] = Path(
        "seed/sample_repo"
    ),
    hop_limit: Annotated[
        int | None, typer.Option(help="Downstream lineage hop limit (defaults to Settings).")
    ] = None,
) -> None:
    """Run the migration: infer the column mapping (printed for review
    before anything else happens), walk downstream lineage, generate a
    rewrite for every consumer matched to a file in `repo`, write a patch
    per consumer, record status, and mark the old asset deprecated with a
    link to the new one."""
    if ctx.invoked_subcommand is not None:
        return
    if not from_urn or not to_urn:
        typer.echo("Both --from and --to are required.", err=True)
        raise typer.Exit(1)

    from sentinel.agents.migration_copilot.orchestrator import run_migration
    from sentinel.core.config import get_settings
    from sentinel.core.datahub_client import DataHubClient

    settings = get_settings()

    async def _run() -> None:
        with DataHubClient(settings) as client:
            async with client.mcp():
                result = await run_migration(
                    client,
                    settings,
                    from_urn,
                    to_urn,
                    repo_root=repo,
                    hop_limit=hop_limit or settings.sentinel_lineage_hop_limit,
                )

        typer.echo("Inferred column mapping (review before merging any patch):")
        for line in result.plan.review_lines():
            typer.echo(f"  {line}")

        typer.echo(f"\nWrote {len(result.records)} patch(es):")
        for r in result.records:
            typer.echo(f"  [{r.status}] {r.file_path} -> {r.link}")

        if result.unmatched_consumer_urns:
            typer.echo(
                f"\n{len(result.unmatched_consumer_urns)} consumer(s) had no matching "
                f"file in {repo} (reported, not silently dropped):"
            )
            for u in result.unmatched_consumer_urns:
                typer.echo(f"  {u}")

        typer.echo(f"\nDeprecation link written to DataHub: {result.deprecation_written}")
        typer.echo(f"Status tracked at {repo / 'migration_status.json'}")

    asyncio.run(_run())


@app.command()
def enrich(
    urn: Annotated[str, typer.Option(help="URN of the asset to draft documentation for.")],
) -> None:
    """Metadata Enrichment (Tier 2 MVP): draft a description + column
    descriptions grounded strictly in DataHub context (lineage neighbors,
    sample queries, existing column docs) and submit them as PENDING
    proposals. Nothing reaches DataHub until `sentinel proposals accept`."""
    from sentinel.agents.metadata_enrichment.enricher import run_enrichment
    from sentinel.core.config import get_settings
    from sentinel.core.datahub_client import DataHubClient
    from sentinel.core.proposal_engine import ProposalEngine

    settings = get_settings()
    proposal_engine = ProposalEngine(Path(settings.sentinel_proposals_path))

    async def _run() -> None:
        with DataHubClient(settings) as client:
            async with client.mcp():
                result = await run_enrichment(client, proposal_engine, settings, urn)

        if result.refused:
            typer.echo(f"Refused to draft for {urn}:\n  {result.reason}")
            raise typer.Exit(0)

        typer.echo(f"Drafted from evidence: {result.reason}\n")
        typer.echo(f"{len(result.proposals)} proposal(s) submitted as PENDING:")
        for p in result.proposals:
            target = f"{p.change_type}" + (f" ({p.field_path})" if p.field_path else "")
            typer.echo(f"  [{p.id}] {target}: {p.proposed_value[:80]}")
        for col in result.insufficient_columns:
            typer.echo(f"  (no draft) {col.column}: {col.reason}")
        typer.echo(
            f"\nReview with `sentinel proposals list`, then accept/reject by id. "
            f"Store: {settings.sentinel_proposals_path}"
        )

    asyncio.run(_run())


@app.command("ml-check")
def ml_check(
    urn: Annotated[
        str,
        typer.Option(
            help="Asset to check: a dataset URN ('what production models depend on "
            "this?') or an mlModel URN ('what does this model depend on?')."
        ),
    ],
    hop_limit: Annotated[
        int,
        typer.Option(help="Lineage hop limit — ML chains run deep, so this defaults higher."),
    ] = 6,
) -> None:
    """ML Blast Radius (Tier 2 MVP): trace ML lineage between this asset and
    production models, check every asset on those paths for active incidents
    and failing assertions, and raise an incident ON THE MODEL entity (with
    the exact path) if a production model sits downstream of an unhealthy
    asset. On-demand check — see README for a cron/Actions scheduling snippet."""
    from sentinel.agents.ml_blast_radius.checker import run_ml_check
    from sentinel.core.config import get_settings
    from sentinel.core.datahub_client import DataHubClient
    from sentinel.core.incident_engine import IncidentEngine, SeverityRules
    from sentinel.integrations.notifiers.jira import JiraNotifier
    from sentinel.integrations.notifiers.slack import SlackNotifier
    from sentinel.integrations.notifiers.teams import TeamsNotifier

    settings = get_settings()
    severity_rules = SeverityRules.from_yaml(settings.sentinel_severity_rules_path)

    async def _run() -> None:
        with DataHubClient(settings) as client:
            engine = IncidentEngine(
                client,
                severity_rules,
                notifiers=[SlackNotifier(settings), JiraNotifier(), TeamsNotifier()],
            )
            async with client.mcp():
                report = await run_ml_check(client, engine, urn, hop_limit=hop_limit)
        typer.echo(report.to_markdown())

    asyncio.run(_run())


quality_app = typer.Typer(
    help="Quality Checking: evaluate quality_checks.yml, write native "
    "assertions back to DataHub, raise/auto-resolve incidents."
)
app.add_typer(quality_app, name="quality")


@quality_app.command("run")
def quality_run(
    config: Annotated[
        Path | None, typer.Option(help="Path to quality_checks.yml (defaults to Settings).")
    ] = None,
    mode: Annotated[
        str,
        typer.Option(
            help="'ingestion' (DataHub profiling stats, no credentials — the demo "
            "default), 'warehouse' (real SQL via the configured sqlite backend), or "
            "'auto' (warehouse if SENTINEL_WAREHOUSE_SQLITE_PATH is set, else ingestion)."
        ),
    ] = "auto",
    hop_limit: Annotated[
        int | None, typer.Option(help="Blast-radius hop limit for severity (defaults to Settings).")
    ] = None,
) -> None:
    """Run every declared quality check once. Cron-friendly: schedule this
    command (see README for a GitHub Actions scheduled-workflow example);
    exits 1 if any check FAILED so schedulers can alert on it."""
    from sentinel.agents.quality_checker.checker import (
        SqliteWarehouse,
        load_checks,
        run_quality_checks,
    )
    from sentinel.core.config import get_settings
    from sentinel.core.datahub_client import DataHubClient
    from sentinel.core.incident_engine import IncidentEngine, SeverityRules
    from sentinel.integrations.notifiers.jira import JiraNotifier
    from sentinel.integrations.notifiers.slack import SlackNotifier
    from sentinel.integrations.notifiers.teams import TeamsNotifier

    settings = get_settings()
    checks = load_checks(config or Path(settings.sentinel_quality_checks_path))
    severity_rules = SeverityRules.from_yaml(settings.sentinel_severity_rules_path)

    resolved_mode = mode
    warehouse = None
    if mode == "auto":
        resolved_mode = "warehouse" if settings.sentinel_warehouse_sqlite_path else "ingestion"
    if resolved_mode == "warehouse":
        if not settings.sentinel_warehouse_sqlite_path:
            typer.echo("warehouse mode needs SENTINEL_WAREHOUSE_SQLITE_PATH set.", err=True)
            raise typer.Exit(1)
        warehouse = SqliteWarehouse(settings.sentinel_warehouse_sqlite_path)

    async def _run() -> None:
        with DataHubClient(settings) as client:
            engine = IncidentEngine(
                client,
                severity_rules,
                notifiers=[SlackNotifier(settings), JiraNotifier(), TeamsNotifier()],
            )
            async with client.mcp():
                report = await run_quality_checks(
                    client,
                    engine,
                    checks,
                    mode=resolved_mode,
                    warehouse=warehouse,
                    hop_limit=hop_limit or settings.sentinel_lineage_hop_limit,
                )
        typer.echo(report.to_markdown())
        if report.failed:
            raise typer.Exit(1)

    asyncio.run(_run())


proposals_app = typer.Typer(
    help="Review Sentinel-drafted metadata change proposals. Accepting a "
    "proposal is the ONLY path by which drafted metadata reaches DataHub."
)
app.add_typer(proposals_app, name="proposals")


def _load_proposal_engine():
    from sentinel.core.config import get_settings
    from sentinel.core.proposal_engine import ProposalEngine

    settings = get_settings()
    return ProposalEngine(Path(settings.sentinel_proposals_path)), settings


@proposals_app.command("list")
def proposals_list(
    target_urn: Annotated[
        str, typer.Option("--urn", help="Only show proposals targeting this URN.")
    ] = "",
) -> None:
    """List PENDING proposals awaiting human review."""
    engine, _ = _load_proposal_engine()
    pending = engine.list_pending(target_urn or None)
    if not pending:
        typer.echo("No pending proposals.")
        return
    for p in pending:
        field_part = f" field={p.field_path}" if p.field_path else ""
        typer.echo(f"[{p.id}] {p.change_type}{field_part} on {p.target_urn}")
        typer.echo(f"    proposed: {p.proposed_value}")
        typer.echo(f"    rationale: {p.rationale}")


@proposals_app.command("accept")
def proposals_accept(
    proposal_id: Annotated[str, typer.Argument(help="Proposal id from `sentinel proposals list`.")],
    decided_by: Annotated[
        str, typer.Option(help="Who is accepting (for the audit trail).")
    ] = "cli",
) -> None:
    """Accept a proposal — this performs the real DataHub write."""
    from sentinel.core.datahub_client import DataHubClient

    engine, settings = _load_proposal_engine()

    async def _run() -> None:
        with DataHubClient(settings) as client:
            async with client.mcp():
                proposal = await engine.accept(proposal_id, client, decided_by=decided_by)
        typer.echo(f"Accepted {proposal.id}: wrote {proposal.change_type} to {proposal.target_urn}")

    asyncio.run(_run())


@proposals_app.command("reject")
def proposals_reject(
    proposal_id: Annotated[str, typer.Argument(help="Proposal id from `sentinel proposals list`.")],
    decided_by: Annotated[
        str, typer.Option(help="Who is rejecting (for the audit trail).")
    ] = "cli",
) -> None:
    """Reject a proposal — nothing is written to DataHub."""
    engine, _ = _load_proposal_engine()
    proposal = engine.reject(proposal_id, decided_by=decided_by)
    typer.echo(f"Rejected {proposal.id} — no DataHub write performed.")


@migrate_app.command("status")
def migrate_status(
    from_urn: Annotated[
        str, typer.Option("--from", help="Old asset URN (for reference; not used to filter).")
    ] = "",
    repo: Annotated[Path, typer.Option(help="Repo containing migration_status.json.")] = Path(
        "seed/sample_repo"
    ),
) -> None:
    """Reload migration_status.json, refresh real-GitHub PR statuses if a
    GITHUB_TOKEN is configured, and print the current status — the CLI
    refresh this spec calls for instead of a webhook listener."""
    from sentinel.agents.migration_copilot.tracker import MigrationTracker
    from sentinel.agents.pr_impact.github_client import GitHubClient
    from sentinel.core.config import get_settings

    status_path = repo / "migration_status.json"
    if not status_path.exists():
        typer.echo(f"No migration_status.json found at {status_path}.", err=True)
        raise typer.Exit(1)

    tracker = MigrationTracker.load(status_path)
    if from_urn and from_urn != tracker.old_urn:
        typer.echo(
            f"Warning: --from {from_urn!r} does not match the tracked migration's "
            f"old_urn {tracker.old_urn!r}.",
            err=True,
        )

    settings = get_settings()
    github_client = None
    if settings.github_token:
        first_pr_link = next(
            (r.link for r in tracker.records if r.status == "pr_opened" and r.link), None
        )
        if first_pr_link and "github.com" in first_pr_link:
            owner_repo = "/".join(first_pr_link.split("/")[3:5])
            github_client = GitHubClient(settings.github_token, owner_repo)

    tracker.refresh(github_client)
    tracker.save(status_path)
    for line in tracker.summary_lines():
        typer.echo(line)


if __name__ == "__main__":
    app()

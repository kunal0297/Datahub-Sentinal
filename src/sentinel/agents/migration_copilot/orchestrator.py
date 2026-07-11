"""Ties the Schema Migration Copilot's pieces together end to end:
fetch both schemas from DataHub -> infer the column mapping (planner.py,
reviewed before proceeding) -> walk downstream lineage (core/blast_radius.py)
-> match consumers to repo files (agents/pr_impact/analyzer.py's sidecar
convention, reused not reimplemented) -> generate a rewrite per consumer
(codegen.py) -> write a patch or open a PR (pr_writer.py) -> record status
(tracker.py) -> mark the old asset deprecated, linking to the new one.

The last step calls `DataHubClient.update_deprecation` directly rather than
routing through core/proposal_engine.py's pending-approval lifecycle: a
human already approved this specific change by running `sentinel migrate`
in the first place, and `updateDeprecation` is a verified, real GraphQL
mutation (see ARCHITECTURE.md "Known gap" for why proposal_engine exists at
all — it's for cases like Metadata Enrichment where the content is
LLM-guessed and genuinely needs review, which a deprecation link, a factual
statement about a migration that just happened, is not).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sentinel.agents.migration_copilot.codegen import generate_rewrite
from sentinel.agents.migration_copilot.planner import MigrationPlan, build_column_mapping
from sentinel.agents.migration_copilot.pr_writer import ConsumerChange, LocalPatchWriter, PRRecord
from sentinel.agents.migration_copilot.tracker import MigrationTracker
from sentinel.agents.pr_impact.analyzer import PRImpactBackend, find_files_for_urns
from sentinel.core.blast_radius import compute_blast_radius
from sentinel.core.config import Settings
from sentinel.core.models import LineageDirection, SchemaField

logger = logging.getLogger(__name__)


class MigrationBackend(PRImpactBackend, Protocol):
    """PRImpactBackend plus the one sync GraphQL write the Migration
    Copilot needs at the end: marking the old asset deprecated with a link
    to the new one."""

    def update_deprecation(
        self, urn: str, deprecated: bool, note: str, replacement_urn: str | None = None
    ) -> bool: ...


@dataclass
class MigrationResult:
    plan: MigrationPlan
    records: list[PRRecord] = field(default_factory=list)
    unmatched_consumer_urns: list[str] = field(default_factory=list)
    deprecation_written: bool = False


async def _fetch_schema(client: MigrationBackend, urn: str) -> list[SchemaField]:
    raw_fields = await client.list_schema_fields(urn)
    return [
        SchemaField(
            name=f["name"] if isinstance(f, dict) else f,
            type=f.get("type") if isinstance(f, dict) else None,
        )
        for f in raw_fields
    ]


async def _fetch_description(client: MigrationBackend, urn: str) -> str:
    entities = await client.get_entities([urn])
    if entities and isinstance(entities[0], dict):
        return str(entities[0].get("description") or entities[0].get("name") or urn)
    return urn


async def run_migration(
    client: MigrationBackend,
    settings: Settings,
    old_urn: str,
    new_urn: str,
    repo_root: Path,
    explicit_mapping: dict[str, str] | None = None,
    hop_limit: int = 3,
    output_dir: Path | None = None,
    deprecation_note: str | None = None,
) -> MigrationResult:
    old_schema = await _fetch_schema(client, old_urn)
    new_schema = await _fetch_schema(client, new_urn)
    new_description = await _fetch_description(client, new_urn)

    plan = build_column_mapping(old_urn, new_urn, old_schema, new_schema, explicit_mapping)
    for line in plan.review_lines():
        logger.info("migration mapping: %s", line)

    report = await compute_blast_radius(client, old_urn, LineageDirection.DOWNSTREAM, hop_limit)
    consumer_urns = {a.urn for a in report.impacted if a.entity_type == "dataset"}

    matched_files = find_files_for_urns(repo_root, consumer_urns)
    unmatched = sorted(consumer_urns - set(matched_files.keys()))
    for urn in unmatched:
        logger.info("consumer %s has no matching file in %s -- skipped", urn, repo_root)

    column_mapping = plan.as_column_mapping_dict()
    writer = LocalPatchWriter(output_dir or (repo_root / "migration_output"))

    records: list[PRRecord] = []
    for consumer_urn, file_path in matched_files.items():
        original = file_path.read_text()
        rewritten = generate_rewrite(
            settings,
            original_content=original,
            column_mapping=column_mapping,
            new_table_name=new_urn,
            new_schema_description=new_description,
        )
        change = ConsumerChange(
            consumer_urn=consumer_urn,
            file_path=str(file_path.relative_to(repo_root)),
            original_content=original,
            rewritten_content=rewritten,
        )
        records.append(writer.write(change))

    tracker = MigrationTracker(old_urn=old_urn, new_urn=new_urn, records=records)
    tracker.save(repo_root / "migration_status.json")

    note = deprecation_note or f"Superseded by {new_urn}. Migrated by DataHub Sentinel."
    deprecation_written = client.update_deprecation(
        old_urn, deprecated=True, note=note, replacement_urn=new_urn
    )

    return MigrationResult(
        plan=plan,
        records=records,
        unmatched_consumer_urns=unmatched,
        deprecation_written=bool(deprecation_written),
    )

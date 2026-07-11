"""Metadata Enrichment (Tier 2 MVP): draft descriptions for an
under-documented asset, grounded strictly in what DataHub actually knows
about it — lineage neighbors' descriptions, sample queries, existing
column-level docs — and submit the draft as a *pending* proposal via
core/proposal_engine.py. Nothing this agent produces reaches DataHub until
a human runs `sentinel proposals accept <id>`; that gate is the feature,
not an inconvenience, because LLM-guessed business meaning is exactly the
kind of metadata that must not silently self-publish.

Two refusal layers keep the output honest:

1. **Context sufficiency** (`assess_context`) — if DataHub has no lineage
   neighbors with descriptions, no sample queries, and no existing column
   docs to ground a draft in, the agent refuses to draft at all and says
   why, rather than emitting confident boilerplate ("this table contains
   order data").
2. **Per-column refusal** — the drafting prompt instructs the model to mark
   any column whose meaning the evidence doesn't support as insufficient
   (with a reason) instead of guessing, and `parse_draft` preserves those
   refusals so the CLI can show them. Columns the model invents (not in the
   real schema) are discarded and logged, mirroring the Migration Copilot's
   "never let the LLM invent column names" rule.

MVP scope note (see README "Known limitations"): this enriches one URN per
invocation. Scheduled/batch enrichment across a whole catalog, and a
prioritization score for which undocumented tables matter most, are natural
next steps.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from sentinel.core.blast_radius import fetch_assets
from sentinel.core.config import Settings
from sentinel.core.models import Asset, MetadataChangeProposal, SchemaField, Urn
from sentinel.core.proposal_engine import ProposalEngine

logger = logging.getLogger(__name__)

SOURCE_AGENT = "metadata-enrichment"

# TODO(batch-mode): a scheduler that walks search results (e.g. every dataset
# missing a description in a given domain) and calls run_enrichment per hit
# would plug in here — deliberately out of MVP scope, see module docstring.


class EnrichmentBackend(Protocol):
    """The subset of DataHubClient's async MCP-backed surface this agent
    reads from. `FakeDataHubClient` satisfies this structurally."""

    async def get_entities(self, urns: list[str]) -> list[dict[str, Any]]: ...

    async def list_schema_fields(self, urn: str, **kwargs: Any) -> list[Any]: ...

    async def get_lineage(
        self, urn: str, direction: str = "DOWNSTREAM", hops: int = 1, **kwargs: Any
    ) -> list[str]: ...

    async def get_dataset_queries(self, urn: str, **kwargs: Any) -> list[Any]: ...


@dataclass
class EnrichmentContext:
    """Everything the drafting prompt is allowed to see. If it isn't in
    here, the model can't ground on it — which is the point."""

    asset: Asset
    columns: list[SchemaField] = field(default_factory=list)
    upstream: list[Asset] = field(default_factory=list)
    downstream: list[Asset] = field(default_factory=list)
    sample_queries: list[str] = field(default_factory=list)

    @property
    def described_neighbors(self) -> list[Asset]:
        return [a for a in (*self.upstream, *self.downstream) if (a.description or "").strip()]

    @property
    def described_columns(self) -> list[SchemaField]:
        return [c for c in self.columns if (c.description or "").strip()]


def _normalize_schema_fields(raw_fields: list[Any]) -> list[SchemaField]:
    fields = []
    for f in raw_fields:
        if isinstance(f, dict):
            fields.append(
                SchemaField(
                    name=f.get("name") or f.get("fieldPath", ""),
                    type=f.get("type"),
                    description=f.get("description"),
                )
            )
        else:
            fields.append(SchemaField(name=str(f)))
    return [f for f in fields if f.name]


def _normalize_queries(raw_queries: list[Any]) -> list[str]:
    """`get_dataset_queries`' exact payload shape is unverified (same
    contract note as core/blast_radius.py) — accept both plain strings and
    dicts under the likely keys, and drop anything unrecognizable rather
    than crashing the whole enrichment over a payload-shape drift."""
    queries: list[str] = []
    for q in raw_queries:
        if isinstance(q, str) and q.strip():
            queries.append(q.strip())
        elif isinstance(q, dict):
            for key in ("query", "sql", "statement", "queryText"):
                value = q.get(key)
                if isinstance(value, str) and value.strip():
                    queries.append(value.strip())
                    break
    return queries


async def gather_context(client: EnrichmentBackend, urn: str) -> EnrichmentContext:
    """One-hop lineage in both directions (a table's meaning lives in what
    it's built from and what's built on it), plus schema and sample queries."""
    assets = await fetch_assets(client, [urn])
    asset = assets[urn]

    raw_fields = await client.list_schema_fields(urn)
    columns = _normalize_schema_fields(raw_fields)

    upstream_urns = await client.get_lineage(urn, direction="UPSTREAM", hops=1)
    downstream_urns = await client.get_lineage(urn, direction="DOWNSTREAM", hops=1)
    neighbor_assets = await fetch_assets(client, sorted({*upstream_urns, *downstream_urns}))

    raw_queries = await client.get_dataset_queries(urn)
    sample_queries = _normalize_queries(raw_queries)

    return EnrichmentContext(
        asset=asset,
        columns=columns,
        upstream=[neighbor_assets[u] for u in upstream_urns if u in neighbor_assets],
        downstream=[neighbor_assets[u] for u in downstream_urns if u in neighbor_assets],
        sample_queries=sample_queries,
    )


def assess_context(context: EnrichmentContext) -> tuple[bool, str]:
    """Returns (sufficient, explanation). Insufficient means: nothing in
    DataHub actually tells us what this asset means, so any draft would be
    invented — refuse instead. The explanation is user-facing either way:
    when sufficient it lists the grounding evidence, when not it lists
    exactly what was missing so the user knows what to ingest to fix it."""
    evidence: list[str] = []
    if context.described_neighbors:
        names = ", ".join(a.name for a in context.described_neighbors[:5])
        evidence.append(
            f"{len(context.described_neighbors)} described lineage neighbor(s): {names}"
        )
    if context.sample_queries:
        evidence.append(f"{len(context.sample_queries)} sample quer(ies)")
    if context.described_columns:
        evidence.append(f"{len(context.described_columns)} column(s) with existing descriptions")

    if evidence:
        return True, "; ".join(evidence)
    return False, (
        "insufficient context to describe confidently: the asset has no lineage "
        "neighbors with descriptions, no sample queries recorded in DataHub, and "
        "no existing column-level descriptions to ground a draft in. Refusing to "
        "draft rather than inventing business meaning the data doesn't support."
    )


SYSTEM_PROMPT = (
    "You are a careful data-catalog documentation assistant. You draft a dataset "
    "description and column descriptions grounded STRICTLY in the evidence "
    "provided: the schema, lineage neighbors' descriptions, and sample queries. "
    "Never invent business meaning the evidence does not support. If the "
    "evidence does not tell you what a column means, do not guess — mark it "
    "insufficient with a short reason instead. Every description you do write "
    "must cite which piece of evidence grounds it. Respond with ONLY a JSON "
    "object, no markdown fences, in exactly this shape:\n"
    "{\n"
    '  "table_description": {"text": "...", "evidence": "..."} or null if insufficient,\n'
    '  "column_descriptions": [\n'
    '    {"column": "<name>", "text": "...", "evidence": "..."},\n'
    '    {"column": "<name>", "text": null, "reason": "insufficient context because ..."}\n'
    "  ]\n"
    "}"
)


def build_enrichment_prompt(context: EnrichmentContext) -> str:
    """Pure and independently testable, like the Migration Copilot's
    `build_codegen_prompt`: the property that matters — the model only ever
    sees real column names and real DataHub evidence — is verifiable
    without an API call."""
    lines = [
        "## Asset",
        f"URN: {context.asset.urn}",
        f"Name: {context.asset.name}",
        f"Existing description: {context.asset.description or '(none)'}",
        "",
        "## Columns (draft a description for each, or mark it insufficient)",
    ]
    for col in context.columns:
        type_part = f" ({col.type})" if col.type else ""
        desc_part = f" — existing description: {col.description}" if col.description else ""
        lines.append(f"- {col.name}{type_part}{desc_part}")

    lines.append("")
    lines.append("## Lineage neighbors (evidence)")
    if not context.upstream and not context.downstream:
        lines.append("(none)")
    for label, assets in (("upstream", context.upstream), ("downstream", context.downstream)):
        for a in assets:
            lines.append(f"- [{label}] {a.name}: {a.description or '(no description)'}")

    lines.append("")
    lines.append("## Sample queries against this asset (evidence)")
    if not context.sample_queries:
        lines.append("(none)")
    for q in context.sample_queries[:10]:
        lines.append(f"```sql\n{q}\n```")

    lines.append("")
    lines.append(
        "Draft the JSON described in the system prompt. Only describe the columns "
        "listed above — never any other column name."
    )
    return "\n".join(lines)


@dataclass
class ColumnDraft:
    column: str
    text: str | None
    evidence: str = ""
    reason: str = ""  # set when text is None (per-column refusal)


@dataclass
class EnrichmentDraft:
    table_description: str | None = None
    table_evidence: str = ""
    columns: list[ColumnDraft] = field(default_factory=list)


def parse_draft(raw: str, real_columns: list[str]) -> EnrichmentDraft:
    """Parses the model's JSON and enforces the invented-column guard:
    any drafted column not present in the real DataHub schema is discarded
    (and logged), never proposed."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)

    draft = EnrichmentDraft()
    table = data.get("table_description")
    if isinstance(table, dict) and table.get("text"):
        draft.table_description = str(table["text"])
        draft.table_evidence = str(table.get("evidence", ""))

    real = set(real_columns)
    for item in data.get("column_descriptions", []):
        if not isinstance(item, dict) or not item.get("column"):
            continue
        column = str(item["column"])
        if column not in real:
            logger.warning(
                "discarding drafted description for %r: not a real column in the "
                "DataHub schema (LLM invention guard)",
                column,
            )
            continue
        draft.columns.append(
            ColumnDraft(
                column=column,
                text=item.get("text") if item.get("text") else None,
                evidence=str(item.get("evidence", "")),
                reason=str(item.get("reason", "")),
            )
        )
    return draft


def draft_enrichment(settings: Settings, context: EnrichmentContext) -> EnrichmentDraft:
    """The thin wrapper that actually calls the Anthropic API — everything
    testable lives in `build_enrichment_prompt` and `parse_draft`."""
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None)
    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_enrichment_prompt(context)}],
    )
    raw = "".join(block.text for block in response.content if block.type == "text")
    return parse_draft(raw, [c.name for c in context.columns])


@dataclass
class EnrichmentResult:
    urn: str
    refused: bool
    reason: str  # grounding evidence when drafted; why-not when refused
    proposals: list[MetadataChangeProposal] = field(default_factory=list)
    insufficient_columns: list[ColumnDraft] = field(default_factory=list)


async def run_enrichment(
    client: EnrichmentBackend,
    proposal_engine: ProposalEngine,
    settings: Settings,
    urn: str,
    drafter: Any = None,
) -> EnrichmentResult:
    """Gather -> assess -> draft -> submit as PENDING proposals. Submitting
    is the only write this function performs, and it goes to the proposal
    store, never to DataHub — see module docstring.

    `drafter` is a test seam (signature of `draft_enrichment`); production
    callers leave it None.
    """
    Urn(raw=urn)  # fail fast on malformed input before any network call
    context = await gather_context(client, urn)

    sufficient, reason = assess_context(context)
    if not sufficient:
        logger.info("enrichment refused for %s: %s", urn, reason)
        return EnrichmentResult(urn=urn, refused=True, reason=reason)

    draft = (drafter or draft_enrichment)(settings, context)

    result = EnrichmentResult(urn=urn, refused=False, reason=reason)
    if draft.table_description:
        result.proposals.append(
            proposal_engine.submit(
                target_urn=urn,
                change_type="description",
                proposed_value=draft.table_description,
                rationale=f"Grounded in: {draft.table_evidence or reason}",
                source_agent=SOURCE_AGENT,
            )
        )
    for col in draft.columns:
        if col.text is None:
            result.insufficient_columns.append(col)
            continue
        result.proposals.append(
            proposal_engine.submit(
                target_urn=urn,
                change_type="column_description",
                proposed_value=col.text,
                rationale=f"Grounded in: {col.evidence or reason}",
                source_agent=SOURCE_AGENT,
                field_path=col.column,
            )
        )

    logger.info(
        "enrichment for %s: %d proposal(s) submitted PENDING, %d column(s) marked insufficient",
        urn,
        len(result.proposals),
        len(result.insufficient_columns),
    )
    return result

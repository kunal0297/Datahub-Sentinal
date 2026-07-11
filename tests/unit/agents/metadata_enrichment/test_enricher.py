"""Unit tests for the Metadata Enrichment agent. The load-bearing
assertions here are the spec's Definition of Done items: proposals are
submitted PENDING and never published directly, the agent refuses when
context is genuinely too thin, and the prompt/parse layers enforce the
"only real DataHub columns, only real DataHub evidence" grounding rules.
"""

from __future__ import annotations

import json

import pytest

from sentinel.agents.metadata_enrichment.enricher import (
    ColumnDraft,
    EnrichmentContext,
    EnrichmentDraft,
    assess_context,
    build_enrichment_prompt,
    gather_context,
    parse_draft,
    run_enrichment,
)
from sentinel.core.config import Settings
from sentinel.core.models import Asset, ProposalStatus, SchemaField
from sentinel.core.proposal_engine import ProposalEngine

URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.mystery_table,PROD)"
UPSTREAM = "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.orders_cleaned,PROD)"
DOWNSTREAM = "urn:li:dashboard:(looker,executive_orders_dashboard)"


def seed_rich_context(fake) -> None:
    fake.entities[URN] = {
        "urn": URN,
        "type": "dataset",
        "name": "analytics.mystery_table",
        "description": "",
    }
    fake.entities[UPSTREAM] = {
        "urn": UPSTREAM,
        "type": "dataset",
        "name": "staging.orders_cleaned",
        "description": "dbt staging model: raw.orders joined with raw.payments.",
    }
    fake.entities[DOWNSTREAM] = {
        "urn": DOWNSTREAM,
        "type": "dashboard",
        "name": "executive_orders_dashboard",
        "description": "Company-wide orders and revenue overview.",
    }
    fake.lineage[URN] = {"UPSTREAM": [UPSTREAM], "DOWNSTREAM": [DOWNSTREAM]}
    fake.schema_fields[URN] = [
        {"name": "order_id", "type": "varchar", "description": "Primary key."},
        {"name": "net_revenue", "type": "numeric"},
    ]
    fake.queries[URN] = [
        {"query": "SELECT order_id, net_revenue FROM analytics.mystery_table WHERE ..."}
    ]


def seed_thin_context(fake) -> None:
    """An asset DataHub knows nothing useful about: no described neighbors,
    no queries, no column descriptions."""
    fake.entities[URN] = {"urn": URN, "type": "dataset", "name": "analytics.mystery_table"}
    fake.lineage[URN] = {"UPSTREAM": [], "DOWNSTREAM": []}
    fake.schema_fields[URN] = [{"name": "col_a"}, {"name": "col_b"}]
    fake.queries[URN] = []


class TestGatherAndAssess:
    @pytest.mark.asyncio
    async def test_gather_context_assembles_all_evidence(self, fake_datahub):
        seed_rich_context(fake_datahub)
        context = await gather_context(fake_datahub, URN)
        assert context.asset.name == "analytics.mystery_table"
        assert [c.name for c in context.columns] == ["order_id", "net_revenue"]
        assert [a.urn for a in context.upstream] == [UPSTREAM]
        assert [a.urn for a in context.downstream] == [DOWNSTREAM]
        assert context.sample_queries and "net_revenue" in context.sample_queries[0]

    @pytest.mark.asyncio
    async def test_assess_sufficient_lists_evidence(self, fake_datahub):
        seed_rich_context(fake_datahub)
        context = await gather_context(fake_datahub, URN)
        sufficient, reason = assess_context(context)
        assert sufficient
        assert "lineage neighbor" in reason
        assert "sample quer" in reason

    @pytest.mark.asyncio
    async def test_assess_insufficient_explains_why(self, fake_datahub):
        seed_thin_context(fake_datahub)
        context = await gather_context(fake_datahub, URN)
        sufficient, reason = assess_context(context)
        assert not sufficient
        assert "insufficient context" in reason
        assert "no lineage neighbors with descriptions" in reason


class TestPromptConstruction:
    def test_prompt_contains_only_real_columns_and_evidence(self):
        context = EnrichmentContext(
            asset=Asset(urn=URN, entity_type="dataset", name="analytics.mystery_table"),
            columns=[SchemaField(name="order_id"), SchemaField(name="net_revenue")],
            upstream=[
                Asset(
                    urn=UPSTREAM,
                    entity_type="dataset",
                    name="staging.orders_cleaned",
                    description="dbt staging model.",
                )
            ],
            sample_queries=["SELECT order_id FROM t"],
        )
        prompt = build_enrichment_prompt(context)
        assert "- order_id" in prompt
        assert "- net_revenue" in prompt
        assert "staging.orders_cleaned: dbt staging model." in prompt
        assert "SELECT order_id FROM t" in prompt
        # the instruction that scopes the model to the listed columns
        assert "Only describe the columns" in prompt


class TestParseDraft:
    def test_parses_table_and_column_drafts(self):
        raw = json.dumps(
            {
                "table_description": {"text": "Orders rollup.", "evidence": "upstream model"},
                "column_descriptions": [
                    {"column": "order_id", "text": "PK.", "evidence": "existing description"},
                    {"column": "net_revenue", "text": None, "reason": "insufficient context"},
                ],
            }
        )
        draft = parse_draft(raw, ["order_id", "net_revenue"])
        assert draft.table_description == "Orders rollup."
        assert draft.columns[0].text == "PK."
        assert draft.columns[1].text is None
        assert "insufficient" in draft.columns[1].reason

    def test_discards_invented_columns(self, caplog):
        raw = json.dumps(
            {
                "table_description": None,
                "column_descriptions": [
                    {"column": "totally_made_up", "text": "Invented.", "evidence": "none"},
                    {"column": "order_id", "text": "PK.", "evidence": "schema"},
                ],
            }
        )
        draft = parse_draft(raw, ["order_id"])
        assert [c.column for c in draft.columns] == ["order_id"]

    def test_strips_markdown_fences(self):
        raw = '```json\n{"table_description": null, "column_descriptions": []}\n```'
        draft = parse_draft(raw, [])
        assert draft.table_description is None


class TestRunEnrichment:
    @pytest.mark.asyncio
    async def test_proposals_are_pending_and_nothing_is_published(self, fake_datahub, tmp_path):
        """The spec's headline DoD: proposals land as PENDING in the store,
        and no DataHub write (update_description) happens during the run."""
        seed_rich_context(fake_datahub)
        engine = ProposalEngine(tmp_path / "proposals.json")

        def fake_drafter(settings, context):
            return EnrichmentDraft(
                table_description="Per-customer revenue rollup built from orders_cleaned.",
                table_evidence="upstream staging.orders_cleaned description",
                columns=[
                    ColumnDraft(column="order_id", text="Primary key.", evidence="schema"),
                    ColumnDraft(column="net_revenue", text=None, reason="insufficient context"),
                ],
            )

        result = await run_enrichment(fake_datahub, engine, Settings(), URN, drafter=fake_drafter)

        assert not result.refused
        assert len(result.proposals) == 2  # table + order_id (net_revenue refused per-column)
        assert all(p.status == ProposalStatus.PENDING for p in result.proposals)
        assert [c.column for c in result.insufficient_columns] == ["net_revenue"]
        # nothing was published to DataHub
        assert not [c for c in fake_datahub.calls if c[0] == "update_description"]
        # and the store round-trips as pending
        reloaded = ProposalEngine(tmp_path / "proposals.json")
        assert len(reloaded.list_pending(URN)) == 2

    @pytest.mark.asyncio
    async def test_refuses_on_thin_context_without_calling_llm(self, fake_datahub, tmp_path):
        seed_thin_context(fake_datahub)
        engine = ProposalEngine(tmp_path / "proposals.json")

        def exploding_drafter(settings, context):
            raise AssertionError("LLM must not be called when context is insufficient")

        result = await run_enrichment(
            fake_datahub, engine, Settings(), URN, drafter=exploding_drafter
        )
        assert result.refused
        assert "insufficient context" in result.reason
        assert result.proposals == []
        assert engine.list_pending() == []

    @pytest.mark.asyncio
    async def test_accepting_a_proposal_is_what_writes_to_datahub(self, fake_datahub, tmp_path):
        """The human gate end-to-end: run -> accept -> the write happens."""
        seed_rich_context(fake_datahub)
        engine = ProposalEngine(tmp_path / "proposals.json")

        def fake_drafter(settings, context):
            return EnrichmentDraft(table_description="Rollup table.", table_evidence="lineage")

        result = await run_enrichment(fake_datahub, engine, Settings(), URN, drafter=fake_drafter)
        proposal = result.proposals[0]
        await engine.accept(proposal.id, fake_datahub, decided_by="tester")
        writes = [c for c in fake_datahub.calls if c[0] == "update_description"]
        assert len(writes) == 1
        assert writes[0][1]["urn"] == URN

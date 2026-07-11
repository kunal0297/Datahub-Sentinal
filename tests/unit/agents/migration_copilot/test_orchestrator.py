import json
import shutil
from pathlib import Path

import pytest

from sentinel.agents.migration_copilot.orchestrator import run_migration
from sentinel.core.config import Settings

REAL_SAMPLE_REPO = Path(__file__).parents[4] / "seed" / "sample_repo"

ORDERS_V1 = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v1,PROD)"
ORDERS_V2 = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v2,PROD)"
REVENUE_SUMMARY = (
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customer_revenue_summary,PROD)"
)
UNMATCHED_CONSUMER = "urn:li:dataset:(urn:li:dataPlatform:snowflake,some_other_repo_table,PROD)"


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """A throwaway copy of the real seed/sample_repo files, so the test can
    let run_migration write migration_status.json and patch files into it
    without touching the actual tracked repo."""
    dest = tmp_path / "sample_repo"
    shutil.copytree(REAL_SAMPLE_REPO, dest)
    return dest


@pytest.fixture
def fake_generate_rewrite(monkeypatch):
    """Stubs the LLM call -- the Definition of Done tests prompt
    construction (see test_codegen.py), not a live API call. The stub
    returns a fixed, obviously-fake rewrite so the test can assert the
    orchestrator wired the real inputs through to it and wrote the result
    back out correctly."""
    calls = []

    def fake(settings, original_content, column_mapping, new_table_name, new_schema_description):
        calls.append(
            {
                "original_content": original_content,
                "column_mapping": column_mapping,
                "new_table_name": new_table_name,
            }
        )
        return "select 1 -- rewritten by fake LLM"

    monkeypatch.setattr("sentinel.agents.migration_copilot.orchestrator.generate_rewrite", fake)
    return calls


class TestRunMigration:
    @pytest.mark.asyncio
    async def test_end_to_end_against_real_sample_repo_files(
        self, fake_datahub, repo_copy, fake_generate_rewrite
    ):
        fake_datahub.schema_fields = {
            ORDERS_V1: [
                {"name": "order_id", "type": "varchar"},
                {"name": "customer_id", "type": "varchar"},
                {"name": "order_date", "type": "timestamp"},
                {"name": "total_amount", "type": "numeric"},
                {"name": "discount_pct", "type": "numeric"},
                {"name": "status", "type": "varchar"},
            ],
            ORDERS_V2: [
                {"name": "order_id", "type": "varchar"},
                {"name": "customer_id", "type": "varchar"},
                {"name": "order_date", "type": "timestamp"},
                {"name": "total_amount_usd", "type": "numeric"},
                {"name": "discount_percentage", "type": "numeric"},
                {"name": "order_status", "type": "varchar"},
                {"name": "currency", "type": "varchar"},
            ],
        }
        fake_datahub.lineage = {
            ORDERS_V1: {"DOWNSTREAM": [REVENUE_SUMMARY, UNMATCHED_CONSUMER]},
        }
        fake_datahub.entities = {
            ORDERS_V2: {"urn": ORDERS_V2, "type": "dataset", "description": "orders v2"},
            REVENUE_SUMMARY: {"urn": REVENUE_SUMMARY, "type": "dataset", "name": "revenue summary"},
            UNMATCHED_CONSUMER: {"urn": UNMATCHED_CONSUMER, "type": "dataset", "name": "elsewhere"},
        }

        settings = Settings(_env_file=None)
        result = await run_migration(
            fake_datahub, settings, ORDERS_V1, ORDERS_V2, repo_root=repo_copy
        )

        # column mapping recovered without hints (same as test_planner.py)
        by_old = {m.old_column: m.new_column for m in result.plan.mappings}
        assert by_old["total_amount"] == "total_amount_usd"
        assert by_old["discount_pct"] == "discount_percentage"

        # only the consumer with a real matching file got a patch; the other
        # is reported, not silently dropped
        assert len(result.records) == 1
        assert result.records[0].consumer_urn == REVENUE_SUMMARY
        assert result.unmatched_consumer_urns == [UNMATCHED_CONSUMER]

        # the fake LLM was actually invoked with the real original file content
        assert len(fake_generate_rewrite) == 1
        assert "total_amount" in fake_generate_rewrite[0]["original_content"]
        assert fake_generate_rewrite[0]["column_mapping"]["total_amount"] == "total_amount_usd"

        # a real patch file landed on disk
        patch_path = Path(result.records[0].link)
        assert patch_path.exists()
        assert "rewritten by fake LLM" in patch_path.read_text()

        # migration_status.json is real and loadable
        status_path = repo_copy / "migration_status.json"
        assert status_path.exists()
        status = json.loads(status_path.read_text())
        assert status["old_urn"] == ORDERS_V1
        assert status["new_urn"] == ORDERS_V2
        assert len(status["records"]) == 1

        # the old asset was marked deprecated, linked to the new one
        assert result.deprecation_written is True
        dep_call = next(c for c in fake_datahub.calls if c[0] == "update_deprecation")
        assert dep_call[1]["urn"] == ORDERS_V1
        assert dep_call[1]["deprecated"] is True
        assert dep_call[1]["replacement_urn"] == ORDERS_V2

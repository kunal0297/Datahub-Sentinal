import pytest

from sentinel.core.blast_radius import compute_blast_radius, walk_lineage
from sentinel.core.models import LineageDirection

ORDERS_V1 = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v1,PROD)"
REVENUE_CHART = "urn:li:chart:(looker,orders_revenue_chart)"
EXEC_DASHBOARD = "urn:li:dashboard:(looker,executive_orders_dashboard)"
REGIONAL_DASHBOARD = "urn:li:dashboard:(looker,regional_sales_dashboard)"
FEATURE_TABLE = "urn:li:mlFeatureTable:(urn:li:dataPlatform:feast,customer_ltv_features)"
FRAUD_MODEL = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detection_v3,PROD)"
ORDERS_V2 = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v2,PROD)"


def _seed_orders_v1_lineage(fake_datahub):
    """Mirrors seed/seed_datahub.py's real shape: orders_v1 feeds a chart and
    two dashboards; the chart also feeds the exec dashboard (a diamond, so
    dedup-across-paths is exercised too)."""
    fake_datahub.lineage = {
        ORDERS_V1: {"DOWNSTREAM": [REVENUE_CHART, REGIONAL_DASHBOARD]},
        REVENUE_CHART: {"DOWNSTREAM": [EXEC_DASHBOARD]},
    }
    fake_datahub.entities = {
        ORDERS_V1: {
            "urn": ORDERS_V1,
            "type": "dataset",
            "name": "analytics.orders_v1",
            "tags": ["production-critical"],
        },
        REVENUE_CHART: {"urn": REVENUE_CHART, "type": "chart", "name": "orders_revenue_chart"},
        EXEC_DASHBOARD: {
            "urn": EXEC_DASHBOARD,
            "type": "dashboard",
            "name": "executive_orders_dashboard",
        },
        REGIONAL_DASHBOARD: {
            "urn": REGIONAL_DASHBOARD,
            "type": "dashboard",
            "name": "regional_sales_dashboard",
        },
    }


class TestWalkLineage:
    @pytest.mark.asyncio
    async def test_bfs_respects_hop_limit(self, fake_datahub):
        _seed_orders_v1_lineage(fake_datahub)

        edges_1hop = await walk_lineage(fake_datahub, ORDERS_V1, LineageDirection.DOWNSTREAM, 1)
        reached_1hop = {e.target_urn for e in edges_1hop}
        assert reached_1hop == {REVENUE_CHART, REGIONAL_DASHBOARD}

        edges_2hop = await walk_lineage(fake_datahub, ORDERS_V1, LineageDirection.DOWNSTREAM, 2)
        reached_2hop = {e.target_urn for e in edges_2hop}
        assert reached_2hop == {REVENUE_CHART, REGIONAL_DASHBOARD, EXEC_DASHBOARD}

    @pytest.mark.asyncio
    async def test_stops_early_when_no_new_nodes(self, fake_datahub):
        _seed_orders_v1_lineage(fake_datahub)
        edges = await walk_lineage(fake_datahub, ORDERS_V1, LineageDirection.DOWNSTREAM, 10)
        # should have stopped after hop 2, not spun through 10 empty hops
        assert max(e.hops for e in edges) == 2

    @pytest.mark.asyncio
    async def test_leaf_node_has_no_edges(self, fake_datahub):
        fake_datahub.lineage = {}
        edges = await walk_lineage(fake_datahub, ORDERS_V1, LineageDirection.DOWNSTREAM, 3)
        assert edges == []


class TestComputeBlastRadius:
    @pytest.mark.asyncio
    async def test_impacted_assets_annotated_with_type(self, fake_datahub):
        _seed_orders_v1_lineage(fake_datahub)
        report = await compute_blast_radius(
            fake_datahub, ORDERS_V1, LineageDirection.DOWNSTREAM, hop_limit=3
        )
        assert set(report.impacted_urns) == {REVENUE_CHART, REGIONAL_DASHBOARD, EXEC_DASHBOARD}
        assert report.downstream_dashboard_count == 3  # 2 dashboards + 1 chart, both counted
        assert report.source_asset.is_production_critical is True

    @pytest.mark.asyncio
    async def test_diamond_path_counts_asset_once(self, fake_datahub):
        _seed_orders_v1_lineage(fake_datahub)
        report = await compute_blast_radius(
            fake_datahub, ORDERS_V1, LineageDirection.DOWNSTREAM, hop_limit=3
        )
        assert report.impacted_urns.count(EXEC_DASHBOARD) == 1

    @pytest.mark.asyncio
    async def test_hops_to_reports_shortest_path(self, fake_datahub):
        _seed_orders_v1_lineage(fake_datahub)
        report = await compute_blast_radius(
            fake_datahub, ORDERS_V1, LineageDirection.DOWNSTREAM, hop_limit=3
        )
        assert report.hops_to(REVENUE_CHART) == 1
        assert report.hops_to(EXEC_DASHBOARD) == 2
        assert report.hops_to("urn:li:dataset:(urn:li:dataPlatform:x,unreached,PROD)") is None

    @pytest.mark.asyncio
    async def test_missing_entity_gets_placeholder_asset_not_dropped(self, fake_datahub):
        """PR Impact Analysis's 'always report what you could and couldn't
        analyze' principle: an urn DataHub has no entity record for should
        still show up as an impacted asset, just a minimal one."""
        fake_datahub.lineage = {
            ORDERS_V1: {"DOWNSTREAM": ["urn:li:dataset:(urn:li:dataPlatform:x,ghost,PROD)"]}
        }
        fake_datahub.entities = {}
        report = await compute_blast_radius(
            fake_datahub, ORDERS_V1, LineageDirection.DOWNSTREAM, hop_limit=1
        )
        assert len(report.impacted) == 1
        assert report.impacted[0].entity_type == "dataset"
        assert report.source_asset.urn == ORDERS_V1

    @pytest.mark.asyncio
    async def test_feeds_production_ml_model_true_when_reached(self, fake_datahub):
        fake_datahub.lineage = {
            ORDERS_V2: {"DOWNSTREAM": [FEATURE_TABLE]},
            FEATURE_TABLE: {"DOWNSTREAM": [FRAUD_MODEL]},
        }
        fake_datahub.entities = {
            ORDERS_V2: {"urn": ORDERS_V2, "type": "dataset", "name": "analytics.orders_v2"},
            FEATURE_TABLE: {
                "urn": FEATURE_TABLE,
                "type": "mlFeatureTable",
                "name": "customer_ltv_features",
            },
            FRAUD_MODEL: {
                "urn": FRAUD_MODEL,
                "type": "mlModel",
                "name": "fraud_detection_v3",
                "tags": ["production"],
            },
        }
        report = await compute_blast_radius(
            fake_datahub, ORDERS_V2, LineageDirection.DOWNSTREAM, hop_limit=3
        )
        assert report.feeds_production_ml_model is True

    @pytest.mark.asyncio
    async def test_to_severity_context_reflects_report(self, fake_datahub):
        _seed_orders_v1_lineage(fake_datahub)
        report = await compute_blast_radius(
            fake_datahub, ORDERS_V1, LineageDirection.DOWNSTREAM, hop_limit=3
        )
        ctx = report.to_severity_context()
        assert ctx.has_production_critical_tag is True
        assert ctx.downstream_dashboard_count == 3
        assert ctx.feeds_production_ml_model is False

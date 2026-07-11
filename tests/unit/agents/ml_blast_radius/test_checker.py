"""Unit tests for the ML Blast Radius checker. The spec's DoD calls out
that the lineage walk across ML entity types must be tested as its own
logic (not relabeled PR Impact code) — `TestBuildMLPaths` covers the typed
path reconstruction directly, and the end-to-end tests mirror the seeded
demo scenario: raw table with a failing freshness assertion feeding a
production fraud model four hops away.
"""

from __future__ import annotations

import pytest

from sentinel.agents.ml_blast_radius.checker import (
    build_ml_paths,
    check_asset_health,
    is_production_model,
    run_ml_check,
)
from sentinel.core.incident_engine import IncidentEngine, SeverityRules
from sentinel.core.models import Asset, Severity

RAW_ORDERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.orders,PROD)"
STAGING = "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.orders_cleaned,PROD)"
ORDERS_V2 = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v2,PROD)"
LTV_FEATURE = "urn:li:mlFeature:(customer_ltv_features,ltv_30d)"
FRAUD_MODEL = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detection_v3,PROD)"
DASHBOARD = "urn:li:dashboard:(looker,executive_orders_dashboard)"


def rules() -> SeverityRules:
    return SeverityRules.from_yaml("config/severity_rules.yml")


def seed_ml_chain(fake, *, failing_assertion: bool = True, model_tags=("production",)) -> None:
    """raw.orders -> staging -> orders_v2 -> ltv feature -> fraud model,
    plus a dashboard branch off orders_v2 that must NOT show up as a model
    path."""
    fake.entities[RAW_ORDERS] = {
        "urn": RAW_ORDERS,
        "type": "dataset",
        "name": "raw.orders",
        "owners": ["urn:li:corpuser:bob"],
    }
    fake.entities[STAGING] = {"urn": STAGING, "type": "dataset", "name": "staging.orders_cleaned"}
    fake.entities[ORDERS_V2] = {"urn": ORDERS_V2, "type": "dataset", "name": "analytics.orders_v2"}
    fake.entities[LTV_FEATURE] = {"urn": LTV_FEATURE, "type": "mlFeature", "name": "ltv_30d"}
    fake.entities[FRAUD_MODEL] = {
        "urn": FRAUD_MODEL,
        "type": "mlModel",
        "name": "fraud_detection_v3",
        "tags": list(model_tags),
        "owners": ["urn:li:corpuser:bob"],
    }
    fake.entities[DASHBOARD] = {"urn": DASHBOARD, "type": "dashboard", "name": "exec_dashboard"}

    fake.lineage[RAW_ORDERS] = {"DOWNSTREAM": [STAGING], "UPSTREAM": []}
    fake.lineage[STAGING] = {"DOWNSTREAM": [ORDERS_V2], "UPSTREAM": [RAW_ORDERS]}
    fake.lineage[ORDERS_V2] = {"DOWNSTREAM": [LTV_FEATURE, DASHBOARD], "UPSTREAM": [STAGING]}
    fake.lineage[LTV_FEATURE] = {"DOWNSTREAM": [FRAUD_MODEL], "UPSTREAM": [ORDERS_V2]}
    fake.lineage[FRAUD_MODEL] = {"DOWNSTREAM": [], "UPSTREAM": [LTV_FEATURE]}
    fake.lineage[DASHBOARD] = {"DOWNSTREAM": [], "UPSTREAM": [ORDERS_V2]}

    if failing_assertion:
        fake.assertions[RAW_ORDERS] = [
            {
                "urn": "urn:li:assertion:freshness-raw-orders",
                "type": "FRESHNESS",
                "description": "raw.orders must land by 6 AM UTC daily.",
                "latest_result": "FAILURE",
                "native_results": {"reason": "no new rows since yesterday 23:10 UTC"},
            }
        ]


class TestBuildMLPaths:
    def test_reconstructs_typed_path_to_model_only(self):
        assets = {
            RAW_ORDERS: Asset(urn=RAW_ORDERS, entity_type="dataset", name="raw.orders"),
            LTV_FEATURE: Asset(urn=LTV_FEATURE, entity_type="mlFeature", name="ltv_30d"),
            FRAUD_MODEL: Asset(urn=FRAUD_MODEL, entity_type="mlModel", name="fraud_detection_v3"),
            DASHBOARD: Asset(urn=DASHBOARD, entity_type="dashboard", name="dash"),
        }
        edges = [
            (RAW_ORDERS, LTV_FEATURE),
            (LTV_FEATURE, FRAUD_MODEL),
            (RAW_ORDERS, DASHBOARD),  # non-ML branch, must not appear
        ]
        paths = build_ml_paths(edges, RAW_ORDERS, assets)
        assert len(paths) == 1
        assert [a.entity_type for a in paths[0].assets] == ["dataset", "mlFeature", "mlModel"]
        assert "raw.orders (dataset) -> ltv_30d (mlFeature) -> fraud_detection_v3 (mlModel)" == (
            paths[0].trace()
        )

    def test_multiple_paths_to_same_model(self):
        feature_b = "urn:li:mlFeature:(customer_ltv_features,orders_90d)"
        assets = {
            ORDERS_V2: Asset(urn=ORDERS_V2, entity_type="dataset", name="orders_v2"),
            LTV_FEATURE: Asset(urn=LTV_FEATURE, entity_type="mlFeature", name="ltv_30d"),
            feature_b: Asset(urn=feature_b, entity_type="mlFeature", name="orders_90d"),
            FRAUD_MODEL: Asset(urn=FRAUD_MODEL, entity_type="mlModel", name="fraud"),
        }
        edges = [
            (ORDERS_V2, LTV_FEATURE),
            (ORDERS_V2, feature_b),
            (LTV_FEATURE, FRAUD_MODEL),
            (feature_b, FRAUD_MODEL),
        ]
        paths = build_ml_paths(edges, ORDERS_V2, assets)
        assert len(paths) == 2

    def test_no_model_reached_means_no_paths(self):
        assets = {
            RAW_ORDERS: Asset(urn=RAW_ORDERS, entity_type="dataset", name="raw.orders"),
            DASHBOARD: Asset(urn=DASHBOARD, entity_type="dashboard", name="dash"),
        }
        paths = build_ml_paths([(RAW_ORDERS, DASHBOARD)], RAW_ORDERS, assets)
        assert paths == []


class TestHealthSignals:
    def test_failing_assertion_is_a_signal(self, fake_datahub):
        seed_ml_chain(fake_datahub)
        asset = Asset(urn=RAW_ORDERS, entity_type="dataset", name="raw.orders")
        signals = check_asset_health(fake_datahub, asset)
        assert len(signals) == 1
        assert signals[0].kind == "failing_assertion"
        assert "no new rows" in signals[0].detail

    def test_healthy_dataset_has_no_signals(self, fake_datahub):
        seed_ml_chain(fake_datahub, failing_assertion=False)
        asset = Asset(urn=STAGING, entity_type="dataset", name="staging.orders_cleaned")
        assert check_asset_health(fake_datahub, asset) == []

    def test_non_dataset_entity_types_skip_assertions(self, fake_datahub):
        asset = Asset(urn=LTV_FEATURE, entity_type="mlFeature", name="ltv_30d")
        assert check_asset_health(fake_datahub, asset) == []
        # no assertion lookup was attempted for a non-dataset
        assert not [c for c in fake_datahub.calls if c[0] == "get_assertions_with_latest_run"]


class TestIsProductionModel:
    def test_tagged_production_model(self):
        asset = Asset(urn=FRAUD_MODEL, entity_type="mlModel", name="fraud", tags=["production"])
        assert is_production_model(asset)

    def test_untagged_model_is_not_production(self):
        asset = Asset(urn=FRAUD_MODEL, entity_type="mlModel", name="fraud")
        assert not is_production_model(asset)

    def test_dataset_is_never_a_production_model(self):
        asset = Asset(urn=RAW_ORDERS, entity_type="dataset", name="t", tags=["production"])
        assert not is_production_model(asset)


class TestRunMLCheck:
    @pytest.mark.asyncio
    async def test_traces_risk_from_dataset_to_production_model(self, fake_datahub):
        """The seeded demo scenario: raw.orders' failing freshness assertion
        must be traced 4 hops to fraud_detection_v3 and raise an incident on
        the MODEL entity with the full path in its description."""
        seed_ml_chain(fake_datahub)
        engine = IncidentEngine(fake_datahub, rules())

        report = await run_ml_check(fake_datahub, engine, RAW_ORDERS)

        assert [m.name for m in report.models_reached] == ["fraud_detection_v3"]
        assert len(report.risks) == 1
        risk = report.risks[0]
        assert risk.model.urn == FRAUD_MODEL
        assert "raw.orders (dataset)" in risk.path.trace()
        assert risk.path.trace().endswith("fraud_detection_v3 (mlModel)")

        # incident raised on the model, not the table
        raises = [c for c in fake_datahub.calls if c[0] == "raise_incident"]
        assert len(raises) == 1
        assert raises[0][1]["resource_urn"] == FRAUD_MODEL
        assert "raw.orders" in raises[0][1]["description"]  # the traced path
        assert "[CRITICAL]" in raises[0][1]["title"]  # feeds_production_ml_model rule

    @pytest.mark.asyncio
    async def test_healthy_chain_raises_nothing(self, fake_datahub):
        seed_ml_chain(fake_datahub, failing_assertion=False)
        engine = IncidentEngine(fake_datahub, rules())
        report = await run_ml_check(fake_datahub, engine, RAW_ORDERS)
        assert report.models_reached and not report.risks
        assert not [c for c in fake_datahub.calls if c[0] == "raise_incident"]
        assert "every asset on every path is currently healthy" in report.to_markdown()

    @pytest.mark.asyncio
    async def test_non_production_model_is_reported_but_not_incident(self, fake_datahub):
        seed_ml_chain(fake_datahub, model_tags=())
        engine = IncidentEngine(fake_datahub, rules())
        report = await run_ml_check(fake_datahub, engine, RAW_ORDERS)
        assert report.models_reached  # the path is still traced and shown
        assert not report.risks  # but a non-production model is not an incident
        assert not [c for c in fake_datahub.calls if c[0] == "raise_incident"]

    @pytest.mark.asyncio
    async def test_model_urn_mode_walks_upstream(self, fake_datahub):
        """`sentinel ml-check --urn <model>` answers 'what does this model
        depend on, and is any of it unhealthy?'"""
        seed_ml_chain(fake_datahub)
        engine = IncidentEngine(fake_datahub, rules())

        report = await run_ml_check(fake_datahub, engine, FRAUD_MODEL)

        assert len(report.risks) == 1
        assert report.risks[0].model.urn == FRAUD_MODEL
        # path is expressed source-first even though the walk ran upstream
        assert report.risks[0].path.assets[0].urn == RAW_ORDERS
        raises = [c for c in fake_datahub.calls if c[0] == "raise_incident"]
        assert len(raises) == 1 and raises[0][1]["resource_urn"] == FRAUD_MODEL

    @pytest.mark.asyncio
    async def test_rerun_dedups_instead_of_duplicating(self, fake_datahub):
        seed_ml_chain(fake_datahub)
        engine = IncidentEngine(fake_datahub, rules())
        await run_ml_check(fake_datahub, engine, RAW_ORDERS)
        await run_ml_check(fake_datahub, engine, RAW_ORDERS)
        raises = [c for c in fake_datahub.calls if c[0] == "raise_incident"]
        assert len(raises) == 1  # second run updated, not duplicated
        active = fake_datahub.get_active_incidents(FRAUD_MODEL, "mlModel")
        assert len(active) == 1

    @pytest.mark.asyncio
    async def test_report_severity_is_critical_for_production_model(self, fake_datahub):
        seed_ml_chain(fake_datahub)
        engine = IncidentEngine(fake_datahub, rules())
        report = await run_ml_check(fake_datahub, engine, RAW_ORDERS)
        markdown = report.to_markdown()
        assert "Production models at risk" in markdown
        assert "fraud_detection_v3" in markdown
        assert report.incidents_raised
        # sanity: the classification the engine applied is CRITICAL
        incident_record = fake_datahub.get_active_incidents(FRAUD_MODEL, "mlModel")[0]
        assert incident_record["title"].startswith(f"[{Severity.CRITICAL.value}]")

from pathlib import Path

import pytest

from sentinel.core.incident_engine import (
    IncidentEngine,
    SeverityContext,
    SeverityRules,
    compute_dedup_key,
    resolve_owner,
)
from sentinel.core.models import (
    Asset,
    IncidentCandidate,
    IncidentState,
    IncidentType,
    Owner,
)

RULES_PATH = Path(__file__).parents[3] / "config" / "severity_rules.yml"

ORDERS_V1 = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v1,PROD)"


def make_candidate(raw_signal: str = "column_removed:discount_pct") -> IncidentCandidate:
    return IncidentCandidate(
        resource_urns=[ORDERS_V1],
        incident_type=IncidentType.OPERATIONAL,
        source_agent="pr-impact-analysis",
        raw_signal=raw_signal,
        title="Breaking change in PR #42",
        context="column discount_pct removed, affecting 3 downstream dashboards",
        link="https://github.com/acme/repo/pull/42",
    )


class TestSeverityRulesFromRepoConfig:
    """The Definition of Done requires severity rules be data-driven (a real
    YAML file), not a hardcoded if/else — these tests load the actual
    config/severity_rules.yml shipped in the repo, not an inline fixture."""

    def test_config_file_exists(self):
        assert RULES_PATH.exists(), f"expected {RULES_PATH} to exist"

    def test_production_critical_tag_is_critical(self):
        rules = SeverityRules.from_yaml(RULES_PATH)
        ctx = SeverityContext(has_production_critical_tag=True)
        assert rules.classify(ctx).value == "CRITICAL"

    def test_feeds_production_ml_model_is_critical(self):
        rules = SeverityRules.from_yaml(RULES_PATH)
        ctx = SeverityContext(feeds_production_ml_model=True)
        assert rules.classify(ctx).value == "CRITICAL"

    def test_downstream_dashboard_is_high(self):
        rules = SeverityRules.from_yaml(RULES_PATH)
        ctx = SeverityContext(downstream_dashboard_count=1)
        assert rules.classify(ctx).value == "HIGH"

    def test_many_downstream_datasets_is_high(self):
        rules = SeverityRules.from_yaml(RULES_PATH)
        ctx = SeverityContext(downstream_dataset_count=5)
        assert rules.classify(ctx).value == "HIGH"

    def test_few_downstream_datasets_is_medium(self):
        rules = SeverityRules.from_yaml(RULES_PATH)
        ctx = SeverityContext(downstream_dataset_count=2)
        assert rules.classify(ctx).value == "MEDIUM"

    def test_nothing_matched_falls_back_to_default(self):
        rules = SeverityRules.from_yaml(RULES_PATH)
        ctx = SeverityContext()
        assert rules.classify(ctx).value == "LOW"

    def test_unknown_field_in_rule_raises(self):
        with pytest.raises(ValueError, match="unknown field"):
            SeverityRules.from_dict(
                {
                    "rules": [
                        {"when": {"field": "not_a_real_field", "equals": True}, "severity": "HIGH"}
                    ]
                }
            )


class TestDedup:
    def test_dedup_key_stable_for_same_inputs(self):
        k1 = compute_dedup_key(ORDERS_V1, IncidentType.OPERATIONAL, "column_removed:discount_pct")
        k2 = compute_dedup_key(ORDERS_V1, IncidentType.OPERATIONAL, "column_removed:discount_pct")
        assert k1 == k2

    def test_dedup_key_differs_for_different_signal(self):
        k1 = compute_dedup_key(ORDERS_V1, IncidentType.OPERATIONAL, "column_removed:discount_pct")
        k2 = compute_dedup_key(ORDERS_V1, IncidentType.OPERATIONAL, "column_removed:status")
        assert k1 != k2

    def test_raising_the_same_incident_twice_yields_one_active_incident(self, fake_datahub):
        rules = SeverityRules.from_yaml(RULES_PATH)
        engine = IncidentEngine(fake_datahub, rules)
        candidate = make_candidate()
        ctx = SeverityContext(has_production_critical_tag=True)

        first = engine.raise_or_update(candidate, ctx, entity_type="dataset")
        second = engine.raise_or_update(candidate, ctx, entity_type="dataset")

        assert first.urn == second.urn
        active = fake_datahub.get_active_incidents(ORDERS_V1, "dataset")
        assert len(active) == 1

    def test_different_signal_on_same_asset_raises_a_second_incident(self, fake_datahub):
        rules = SeverityRules.from_yaml(RULES_PATH)
        engine = IncidentEngine(fake_datahub, rules)
        ctx = SeverityContext()

        first = engine.raise_or_update(
            make_candidate("column_removed:discount_pct"), ctx, "dataset"
        )
        second = engine.raise_or_update(make_candidate("column_removed:status"), ctx, "dataset")

        assert first.urn != second.urn
        assert len(fake_datahub.get_active_incidents(ORDERS_V1, "dataset")) == 2

    def test_incident_description_includes_agent_and_reason(self, fake_datahub):
        rules = SeverityRules.from_yaml(RULES_PATH)
        engine = IncidentEngine(fake_datahub, rules)
        incident = engine.raise_or_update(make_candidate(), SeverityContext(), "dataset")
        assert "pr-impact-analysis" in incident.description
        assert "discount_pct removed" in incident.description
        assert "https://github.com/acme/repo/pull/42" in incident.description


class TestAutoResolution:
    def test_resolve_if_cleared_closes_matching_active_incident(self, fake_datahub):
        rules = SeverityRules.from_yaml(RULES_PATH)
        engine = IncidentEngine(fake_datahub, rules)
        engine.raise_or_update(make_candidate("freshness:raw_orders"), SeverityContext(), "dataset")

        resolved = engine.resolve_if_cleared(
            ORDERS_V1,
            entity_type="dataset",
            incident_type=IncidentType.OPERATIONAL,
            raw_signal="freshness:raw_orders",
            resolution_comment="condition cleared: new rows landed at 06:02 UTC",
        )

        assert resolved is True
        active = fake_datahub.get_active_incidents(ORDERS_V1, "dataset")
        assert len(active) == 0
        all_records = fake_datahub.incidents[ORDERS_V1]
        assert all_records[0]["status"]["state"] == IncidentState.RESOLVED.value
        assert "condition cleared" in all_records[0]["resolution_message"]

    def test_resolve_if_cleared_is_a_noop_when_nothing_active(self, fake_datahub):
        rules = SeverityRules.from_yaml(RULES_PATH)
        engine = IncidentEngine(fake_datahub, rules)
        resolved = engine.resolve_if_cleared(
            ORDERS_V1, "dataset", IncidentType.OPERATIONAL, "never-raised", "n/a"
        )
        assert resolved is False


class TestOwnerResolution:
    def test_direct_owner_wins(self):
        asset = Asset(
            urn=ORDERS_V1,
            entity_type="dataset",
            name="analytics.orders_v1",
            owners=[Owner(urn="urn:li:corpuser:alice")],
            domain="Commerce",
        )
        owner = resolve_owner(
            asset,
            domain_owners={"Commerce": [Owner(urn="urn:li:corpGroup:commerce-team")]},
            default_owner=Owner(urn="urn:li:corpuser:default-triage"),
        )
        assert owner.urn == "urn:li:corpuser:alice"

    def test_falls_back_to_domain_owner_when_asset_ownerless(self):
        """Exercises the deliberately-ownerless-asset path called out in the
        Definition of Done — staging.orders_cleaned in the seed data is a
        real example of this shape (in a domain, no direct owner)."""
        asset = Asset(
            urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.orders_cleaned,PROD)",
            entity_type="dataset",
            name="staging.orders_cleaned",
            owners=[],
            domain="Commerce",
        )
        owner = resolve_owner(
            asset,
            domain_owners={"Commerce": [Owner(urn="urn:li:corpGroup:commerce-team")]},
            default_owner=Owner(urn="urn:li:corpuser:default-triage"),
        )
        assert owner.urn == "urn:li:corpGroup:commerce-team"

    def test_falls_back_to_default_when_no_owner_and_no_domain_match(self):
        asset = Asset(
            urn="urn:li:dataset:(urn:li:dataPlatform:postgres,raw.payments,PROD)",
            entity_type="dataset",
            name="raw.payments",
            owners=[],
            domain=None,
        )
        owner = resolve_owner(
            asset,
            domain_owners={"Commerce": [Owner(urn="urn:li:corpGroup:commerce-team")]},
            default_owner=Owner(urn="urn:li:corpuser:default-triage"),
        )
        assert owner.urn == "urn:li:corpuser:default-triage"

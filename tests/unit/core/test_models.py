import pytest
from pydantic import ValidationError

from sentinel.core.models import (
    IncidentCandidate,
    IncidentType,
    Urn,
    make_dataset_urn,
    make_ml_feature_table_urn,
    make_ml_feature_urn,
    make_ml_model_urn,
)


def test_urn_requires_datahub_prefix():
    with pytest.raises(ValidationError):
        Urn(raw="not-a-urn")


def test_urn_entity_type():
    urn = Urn(raw="urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders,PROD)")
    assert urn.entity_type == "dataset"


def test_urn_equality_and_hash_are_string_based():
    a = Urn(raw="urn:li:dataset:(urn:li:dataPlatform:snowflake,x,PROD)")
    b = Urn(raw="urn:li:dataset:(urn:li:dataPlatform:snowflake,x,PROD)")
    assert a == b
    assert a == b.raw
    assert len({a, b}) == 1


def test_make_dataset_urn_format():
    urn = make_dataset_urn("snowflake", "analytics.orders", "PROD")
    assert urn.raw == "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders,PROD)"


def test_make_ml_model_urn_format():
    urn = make_ml_model_urn("sagemaker", "fraud_detection_v3")
    assert urn.raw == "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detection_v3,PROD)"


def test_make_ml_feature_table_urn_has_no_env():
    urn = make_ml_feature_table_urn("feast", "customer_ltv_features")
    assert urn.raw == "urn:li:mlFeatureTable:(urn:li:dataPlatform:feast,customer_ltv_features)"


def test_make_ml_feature_urn_has_no_platform():
    urn = make_ml_feature_urn("customer", "ltv_30d")
    assert urn.raw == "urn:li:mlFeature:(customer,ltv_30d)"


def test_incident_candidate_requires_incident_type_enum():
    candidate = IncidentCandidate(
        resource_urns=["urn:li:dataset:(urn:li:dataPlatform:snowflake,x,PROD)"],
        incident_type=IncidentType.OPERATIONAL,
        source_agent="pr-impact-analysis",
        raw_signal="column_removed:discount_pct",
        title="Breaking change in PR #42",
        context="column discount_pct removed",
    )
    assert candidate.incident_type is IncidentType.OPERATIONAL

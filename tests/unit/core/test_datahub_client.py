import httpx
import pytest

from sentinel.core.config import Settings
from sentinel.core.datahub_client import DataHubClient, DataHubGraphQLError
from sentinel.core.models import IncidentState, IncidentType


def _client_with_transport(handler) -> DataHubClient:
    settings = Settings(_env_file=None)
    return DataHubClient(settings, transport=httpx.MockTransport(handler))


def test_raise_incident_posts_verified_mutation_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(200, json={"data": {"raiseIncident": "urn:li:incident:abc"}})

    client = _client_with_transport(handler)
    urn = client.raise_incident(
        resource_urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v1,PROD)",
        incident_type=IncidentType.OPERATIONAL,
        title="[CRITICAL] Breaking change in PR #42",
        description="column discount_pct removed, affecting 3 dashboards",
    )
    assert urn == "urn:li:incident:abc"

    import json

    body = json.loads(captured["body"])
    assert "raiseIncident" in body["query"]
    assert body["variables"]["input"] == {
        "resourceUrn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v1,PROD)",
        "type": "OPERATIONAL",
        "title": "[CRITICAL] Breaking change in PR #42",
        "description": "column discount_pct removed, affecting 3 dashboards",
    }
    # Severity is never sent as a GraphQL field — RaiseIncidentInput has none.
    assert "severity" not in body["variables"]["input"]
    assert "priority" not in body["variables"]["input"]


def test_raise_incident_raises_on_graphql_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "boom"}]})

    client = _client_with_transport(handler)
    with pytest.raises(DataHubGraphQLError):
        client.raise_incident(
            resource_urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,x,PROD)",
            incident_type=IncidentType.OPERATIONAL,
            title="t",
            description="d",
        )


def test_update_incident_status_resolves():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"updateIncidentStatus": True}})

    client = _client_with_transport(handler)
    ok = client.update_incident_status(
        "urn:li:incident:abc", IncidentState.RESOLVED, "condition cleared"
    )
    assert ok is True


def test_get_active_incidents_unknown_entity_type_raises():
    client = _client_with_transport(lambda r: httpx.Response(200, json={"data": {}}))
    with pytest.raises(ValueError, match="no verified GraphQL root query field"):
        client.get_active_incidents("urn:li:glossaryTerm:foo", entity_type="glossaryTerm")


def test_get_active_incidents_parses_dataset_root_field():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "dataset": {
                        "incidents": {
                            "total": 1,
                            "incidents": [
                                {
                                    "urn": "urn:li:incident:abc",
                                    "incidentType": "OPERATIONAL",
                                    "title": "t",
                                    "description": "d",
                                    "status": {"state": "ACTIVE"},
                                }
                            ],
                        }
                    }
                }
            },
        )

    client = _client_with_transport(handler)
    incidents = client.get_active_incidents(
        "urn:li:dataset:(urn:li:dataPlatform:snowflake,x,PROD)", entity_type="dataset"
    )
    assert len(incidents) == 1
    assert incidents[0]["urn"] == "urn:li:incident:abc"


def test_update_deprecation_includes_replacement_urn_when_given():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(200, json={"data": {"updateDeprecation": True}})

    client = _client_with_transport(handler)
    client.update_deprecation(
        "urn:li:dataset:(urn:li:dataPlatform:snowflake,orders_v1,PROD)",
        deprecated=True,
        note="superseded by orders_v2",
        replacement_urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,orders_v2,PROD)",
    )
    import json

    body = json.loads(captured["body"])
    assert body["variables"]["input"]["replacementUrn"] == (
        "urn:li:dataset:(urn:li:dataPlatform:snowflake,orders_v2,PROD)"
    )

"""Shared test fixtures. `FakeDataHubClient` is the one mocked DataHub double
used across every unit test in this repo (per the project's testing
requirement to define it once rather than mocking ad hoc per file) — it
mirrors `sentinel.core.datahub_client.DataHubClient`'s public method
signatures so engine/agent code under test can't tell the difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from sentinel.core.models import IncidentState, IncidentType


@dataclass
class FakeDataHubClient:
    """In-memory stand-in for DataHubClient. Seed `entities`, `incidents`,
    `lineage`, and `schema_fields` before exercising code under test; assert
    against `calls` afterward to verify the right DataHub operations were
    invoked without a real GMS."""

    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    # resource_urn -> list of active incident dicts
    incidents: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # urn -> {"DOWNSTREAM": [...], "UPSTREAM": [...]}
    lineage: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    schema_fields: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # urn -> list of sample queries (strings or dicts, mirroring the real
    # tool's unverified payload shape — see enricher._normalize_queries)
    queries: dict[str, list[Any]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _next_incident_id: int = 0

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    # -- sync GraphQL-equivalent methods --

    def raise_incident(
        self,
        resource_urn: str,
        incident_type: IncidentType,
        title: str,
        description: str,
        custom_type: str | None = None,
    ) -> str:
        self._next_incident_id += 1
        urn = f"urn:li:incident:fake-{self._next_incident_id}"
        record = {
            "urn": urn,
            "incidentType": incident_type.value,
            "title": title,
            "description": description,
            "status": {"state": IncidentState.ACTIVE.value},
        }
        self.incidents.setdefault(resource_urn, []).append(record)
        self._record(
            "raise_incident",
            resource_urn=resource_urn,
            incident_type=incident_type,
            title=title,
            description=description,
        )
        return urn

    def update_incident_status(self, incident_urn: str, state: IncidentState, message: str) -> bool:
        found = False
        for records in self.incidents.values():
            for r in records:
                if r["urn"] == incident_urn:
                    r["status"] = {"state": state.value}
                    r["resolution_message"] = message
                    found = True
        self._record(
            "update_incident_status", incident_urn=incident_urn, state=state, message=message
        )
        return found

    def get_active_incidents(self, resource_urn: str, entity_type: str) -> list[dict[str, Any]]:
        self._record("get_active_incidents", resource_urn=resource_urn, entity_type=entity_type)
        return [
            r
            for r in self.incidents.get(resource_urn, [])
            if r["status"]["state"] == IncidentState.ACTIVE.value
        ]

    def update_deprecation(
        self, urn: str, deprecated: bool, note: str, replacement_urn: str | None = None
    ) -> bool:
        self._record(
            "update_deprecation",
            urn=urn,
            deprecated=deprecated,
            note=note,
            replacement_urn=replacement_urn,
        )
        entity = self.entities.setdefault(urn, {})
        entity["deprecation"] = {
            "deprecated": deprecated,
            "note": note,
            "replacementUrn": replacement_urn,
        }
        return True

    # -- async MCP-equivalent methods --

    async def get_lineage(
        self, urn: str, direction: str = "DOWNSTREAM", hops: int = 1, **kwargs: Any
    ):
        self._record("get_lineage", urn=urn, direction=direction, hops=hops)
        return self.lineage.get(urn, {}).get(direction, [])

    async def get_entities(self, urns: list[str]):
        self._record("get_entities", urns=urns)
        return [self.entities[u] for u in urns if u in self.entities]

    async def list_schema_fields(self, urn: str, **kwargs: Any):
        self._record("list_schema_fields", urn=urn)
        return self.schema_fields.get(urn, [])

    async def search(self, query: str, num_results: int = 10, **filters: Any):
        self._record("search", query=query, num_results=num_results)
        return []

    async def get_dataset_queries(self, urn: str, **kwargs: Any):
        self._record("get_dataset_queries", urn=urn)
        return self.queries.get(urn, [])

    async def get_lineage_paths_between(self, source_urn: str, target_urn: str):
        self._record("get_lineage_paths_between", source_urn=source_urn, target_urn=target_urn)
        return []

    async def add_tags(self, urn: str, tags: list[str], field_path: str | None = None):
        self._record("add_tags", urn=urn, tags=tags, field_path=field_path)
        return True

    async def add_terms(self, urn: str, terms: list[str], field_path: str | None = None):
        self._record("add_terms", urn=urn, terms=terms, field_path=field_path)
        return True

    async def add_owners(self, urn: str, owner_urns: list[str], **kwargs: Any):
        self._record("add_owners", urn=urn, owner_urns=owner_urns)
        return True

    async def update_description(self, urn: str, description: str, field_path: str | None = None):
        self._record("update_description", urn=urn, description=description, field_path=field_path)
        entity = self.entities.setdefault(urn, {})
        if field_path:
            entity.setdefault("fieldDescriptions", {})[field_path] = description
        else:
            entity["description"] = description
        return True

    def mcp(self):
        return _NullMCPContext(self)


class _NullMCPContext:
    """No-op async context manager — FakeDataHubClient's async methods don't
    need a live subprocess, so entering/exiting this is a formality that
    keeps call sites identical to the real client's `async with client.mcp():`."""

    def __init__(self, client: FakeDataHubClient):
        self._client = client

    async def __aenter__(self) -> FakeDataHubClient:
        return self._client

    async def __aexit__(self, *exc: Any) -> None:
        return None


@pytest.fixture
def fake_datahub() -> FakeDataHubClient:
    return FakeDataHubClient()

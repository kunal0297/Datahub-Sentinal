"""Typed wrapper around every way Sentinel talks to DataHub.

Three transports, one client:

1. **GraphQL** (sync, via httpx) for incidents and deprecation — these are
   not exposed as MCP tools in the verified `mcp-server-datahub` tool list,
   so Sentinel calls `{gms_url}/api/graphql` directly.
2. **MCP tools** (async, via the official `mcp` SDK talking to the
   `mcp-server-datahub` subprocess over stdio) for search, lineage,
   entity/schema lookups, and the mutation tools (add_tags,
   update_description, add_owners, ...). These are the tool names verified
   against the acryldata/mcp-server-datahub README as of this writing:
   search, get_lineage, get_entities, list_schema_fields,
   get_lineage_paths_between, get_dataset_queries, add_tags, remove_tags,
   add_terms, remove_terms, add_owners, remove_owners, set_domains,
   remove_domains, update_description, add_structured_properties,
   remove_structured_properties, get_me. Re-verify with `list_tools` at
   startup (see `verify_tool_surface`) since this surface changes between
   `mcp-server-datahub` versions.
3. **The `acryl-datahub` Python SDK emitter** (sync) for bulk metadata
   emission — used by `seed/seed_datahub.py` to populate the demo graph.
   Emitting hundreds of MCPs one at a time through MCP tool calls would be
   slow and is not what the emitter is for; direct SDK emission is the
   right tool for bulk seeding.

GraphQL mutation shapes below (raiseIncident, updateIncidentStatus,
updateIncident, updateDeprecation) are verified against
docs.datahub.com/docs/api/graphql/mutations and
docs.datahub.com/docs/api/tutorials/incidents. See ARCHITECTURE.md for the
verification notes, including the one confirmed gap: there is no native
propose*/accept*/reject*-proposal mutation in open-source DataHub, which is
why `core/proposal_engine.py` owns that lifecycle itself instead of
delegating to a DataHub primitive.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

import httpx

from sentinel.core.config import Settings
from sentinel.core.models import (
    IncidentState,
    IncidentType,
)

logger = logging.getLogger(__name__)

# GraphQL root query field per entity type — DataHub attaches `incidents` to
# each of these entity types' root query (verified for `dataset`; the
# others follow the same EntityWithRelationships-style pattern DataHub uses
# for Dataset/DataJob/DataFlow/Dashboard/Chart/MLModel incidents). If you add
# support for a new entity type, confirm its root query field name against
# the live GraphQL schema (`datahub graphql` CLI or the GraphiQL explorer)
# before adding it here — do not guess.
_ENTITY_ROOT_QUERY_FIELD = {
    "dataset": "dataset",
    "dataJob": "dataJob",
    "dataFlow": "dataFlow",
    "dashboard": "dashboard",
    "chart": "chart",
    "mlModel": "mlModel",
}


class DataHubGraphQLError(RuntimeError):
    pass


class DataHubClient:
    """Sync GraphQL + SDK-emitter methods are always available. MCP-backed
    methods require using this client as an async context manager, which
    spawns and holds one `mcp-server-datahub` subprocess for the duration of
    the `async with` block::

        client = DataHubClient(settings)
        async with client.mcp():
            lineage = await client.get_lineage(urn, direction="DOWNSTREAM")
    """

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        """`transport` is a test-only seam (inject `httpx.MockTransport` to
        exercise GraphQL query construction without a live GMS); production
        callers never pass it."""
        self.settings = settings
        self._http = httpx.Client(
            base_url=settings.datahub_gms_url,
            headers=self._auth_headers(settings),
            timeout=30.0,
            transport=transport,
        )
        self._mcp_session: Any = None
        self._mcp_stack: AsyncExitStack | None = None

    @staticmethod
    def _auth_headers(settings: Settings) -> dict[str, str]:
        if settings.datahub_gms_token:
            return {"Authorization": f"Bearer {settings.datahub_gms_token}"}
        return {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DataHubClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- #
    # GraphQL: incidents
    # ---------------------------------------------------------------- #

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._http.post("/api/graphql", json={"query": query, "variables": variables or {}})
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise DataHubGraphQLError(str(payload["errors"]))
        return payload["data"]

    def raise_incident(
        self,
        resource_urn: str,
        incident_type: IncidentType,
        title: str,
        description: str,
        custom_type: str | None = None,
    ) -> str:
        """Calls the verified `raiseIncident` mutation. Returns the new
        incident URN. Severity is NOT a field here — see `models.Severity`
        docstring for why; callers fold severity into `title`/`description`
        before calling this."""
        query = """
        mutation raiseIncident($input: RaiseIncidentInput!) {
          raiseIncident(input: $input)
        }
        """
        variables: dict[str, Any] = {
            "input": {
                "resourceUrn": resource_urn,
                "type": incident_type.value,
                "title": title,
                "description": description,
            }
        }
        if custom_type:
            variables["input"]["customType"] = custom_type
        data = self._graphql(query, variables)
        return data["raiseIncident"]

    def update_incident_status(self, incident_urn: str, state: IncidentState, message: str) -> bool:
        query = """
        mutation updateIncidentStatus($urn: String!, $input: IncidentStatusInput!) {
          updateIncidentStatus(urn: $urn, input: $input)
        }
        """
        variables = {"urn": incident_urn, "input": {"state": state.value, "message": message}}
        data = self._graphql(query, variables)
        return bool(data["updateIncidentStatus"])

    def get_active_incidents(self, resource_urn: str, entity_type: str) -> list[dict[str, Any]]:
        """Returns raw incident dicts (urn, incidentType, title, description,
        status.state) currently ACTIVE on the given resource — used by the
        Incident Automation Engine's dedup check. Raises `ValueError` for
        entity types we haven't verified an `incidents` root query field
        for, rather than silently returning an empty (and misleading) list.
        """
        root_field = _ENTITY_ROOT_QUERY_FIELD.get(entity_type)
        if root_field is None:
            raise ValueError(
                f"no verified GraphQL root query field for entity type {entity_type!r}; "
                f"confirm against the live schema before adding it to _ENTITY_ROOT_QUERY_FIELD"
            )
        query = f"""
        query getActiveIncidents($urn: String!) {{
          {root_field}(urn: $urn) {{
            incidents(state: ACTIVE, start: 0, count: 100) {{
              total
              incidents {{
                urn
                incidentType
                title
                description
                status {{ state }}
              }}
            }}
          }}
        }}
        """
        data = self._graphql(query, {"urn": resource_urn})
        entity = data.get(root_field)
        if not entity or not entity.get("incidents"):
            return []
        return entity["incidents"]["incidents"]

    def get_assertions_with_latest_run(self, dataset_urn: str) -> list[dict[str, Any]]:
        """Returns one normalized dict per assertion on the dataset:
        `{urn, type, description, latest_result, native_results}` where
        `latest_result` is the most recent COMPLETE run's result type
        (`SUCCESS`/`FAILURE`) or None if the assertion has never run.

        Query shape verified against the DataHub Assertions API tutorial
        (docs.datahub.com/docs/api/tutorials/assertions):
        `dataset(urn){ assertions(start, count){ assertions { urn info{...}
        runEvents(status: COMPLETE, limit: 1){ runEvents { result {...} } } } } }`.
        Used by the ML Blast Radius checker to spot upstream assets whose
        freshness/quality checks are currently failing."""
        query = """
        query getAssertions($urn: String!) {
          dataset(urn: $urn) {
            assertions(start: 0, count: 100) {
              total
              assertions {
                urn
                info { type description }
                runEvents(status: COMPLETE, limit: 1) {
                  total
                  failed
                  succeeded
                  runEvents {
                    timestampMillis
                    result { type nativeResults { key value } }
                  }
                }
              }
            }
          }
        }
        """
        data = self._graphql(query, {"urn": dataset_urn})
        entity = data.get("dataset")
        if not entity or not entity.get("assertions"):
            return []
        normalized = []
        for assertion in entity["assertions"].get("assertions", []):
            info = assertion.get("info") or {}
            events = (assertion.get("runEvents") or {}).get("runEvents") or []
            latest_result: str | None = None
            native_results: dict[str, str] = {}
            if events:
                result = events[0].get("result") or {}
                latest_result = result.get("type")
                native_results = {
                    nr["key"]: nr["value"] for nr in result.get("nativeResults") or []
                }
            normalized.append(
                {
                    "urn": assertion["urn"],
                    "type": info.get("type"),
                    "description": info.get("description"),
                    "latest_result": latest_result,
                    "native_results": native_results,
                }
            )
        return normalized

    def update_deprecation(
        self, urn: str, deprecated: bool, note: str, replacement_urn: str | None = None
    ) -> bool:
        query = """
        mutation updateDeprecation($input: UpdateDeprecationInput!) {
          updateDeprecation(input: $input)
        }
        """
        variables: dict[str, Any] = {"input": {"urn": urn, "deprecated": deprecated, "note": note}}
        if replacement_urn:
            variables["input"]["replacementUrn"] = replacement_urn
        data = self._graphql(query, variables)
        return bool(data["updateDeprecation"])

    # ---------------------------------------------------------------- #
    # MCP tools (async)
    # ---------------------------------------------------------------- #

    def mcp(self) -> _MCPContext:
        return _MCPContext(self)

    async def _ensure_mcp(self) -> Any:
        if self._mcp_session is None:
            raise RuntimeError(
                "MCP session not started — use `async with client.mcp():` before "
                "calling MCP-backed methods (search, get_lineage, get_entities, ...)"
            )
        return self._mcp_session

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        session = await self._ensure_mcp()
        result = await session.call_tool(name, arguments)
        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP tool {name!r} returned an error: {result}")
        return result

    async def search(self, query: str, num_results: int = 10, **filters: Any) -> Any:
        return await self.call_tool(
            "search", {"query": query, "num_results": num_results, **filters}
        )

    async def get_lineage(
        self, urn: str, direction: str = "DOWNSTREAM", hops: int = 1, **kwargs: Any
    ) -> Any:
        return await self.call_tool(
            "get_lineage", {"urn": urn, "direction": direction, "hops": hops, **kwargs}
        )

    async def get_lineage_paths_between(self, source_urn: str, target_urn: str) -> Any:
        return await self.call_tool(
            "get_lineage_paths_between", {"source_urn": source_urn, "target_urn": target_urn}
        )

    async def get_entities(self, urns: list[str]) -> Any:
        return await self.call_tool("get_entities", {"urns": urns})

    async def list_schema_fields(self, urn: str, **kwargs: Any) -> Any:
        return await self.call_tool("list_schema_fields", {"urn": urn, **kwargs})

    async def get_dataset_queries(self, urn: str, **kwargs: Any) -> Any:
        return await self.call_tool("get_dataset_queries", {"urn": urn, **kwargs})

    async def add_tags(self, urn: str, tags: list[str], field_path: str | None = None) -> Any:
        args: dict[str, Any] = {"urn": urn, "tags": tags}
        if field_path:
            args["field_path"] = field_path
        return await self.call_tool("add_tags", args)

    async def add_terms(self, urn: str, terms: list[str], field_path: str | None = None) -> Any:
        args: dict[str, Any] = {"urn": urn, "terms": terms}
        if field_path:
            args["field_path"] = field_path
        return await self.call_tool("add_terms", args)

    async def add_owners(self, urn: str, owner_urns: list[str], **kwargs: Any) -> Any:
        return await self.call_tool("add_owners", {"urn": urn, "owner_urns": owner_urns, **kwargs})

    async def update_description(
        self, urn: str, description: str, field_path: str | None = None
    ) -> Any:
        args: dict[str, Any] = {"urn": urn, "description": description}
        if field_path:
            args["field_path"] = field_path
        return await self.call_tool("update_description", args)

    async def get_me(self) -> Any:
        return await self.call_tool("get_me", {})

    async def verify_tool_surface(self) -> set[str]:
        """Calls the MCP server's `list_tools` and returns the tool names it
        actually exposes, so callers/tests can assert Sentinel isn't relying
        on a tool name that's drifted. Import guidance in Section 8/this
        module's docstring says: introspect, don't guess."""
        session = await self._ensure_mcp()
        tools = await session.list_tools()
        return {t.name for t in tools.tools}


class _MCPContext:
    """Async context manager that spawns `mcp-server-datahub` over stdio and
    holds the session open for the block's duration."""

    def __init__(self, client: DataHubClient):
        self._client = client

    async def __aenter__(self) -> DataHubClient:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        settings = self._client.settings
        env = {
            "DATAHUB_GMS_URL": settings.datahub_gms_url,
            "TOOLS_IS_MUTATION_ENABLED": "true" if settings.tools_is_mutation_enabled else "false",
        }
        if settings.datahub_gms_token:
            env["DATAHUB_GMS_TOKEN"] = settings.datahub_gms_token

        server_params = StdioServerParameters(command="mcp-server-datahub", args=[], env=env)
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(server_params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        self._client._mcp_stack = stack
        self._client._mcp_session = session
        return self._client

    async def __aexit__(self, *exc: Any) -> None:
        if self._client._mcp_stack is not None:
            await self._client._mcp_stack.aclose()
        self._client._mcp_session = None
        self._client._mcp_stack = None


# ---------------------------------------------------------------------- #
# SDK emitter helpers (sync) — used by seed/seed_datahub.py
# ---------------------------------------------------------------------- #


def make_rest_emitter(settings: Settings):
    """Returns a `DatahubRestEmitter` configured from Settings. Kept as a
    thin factory (rather than a method on DataHubClient) because the
    emitter is a distinct, synchronous, bulk-oriented tool — see module
    docstring point 3."""
    from datahub.emitter.rest_emitter import DatahubRestEmitter

    return DatahubRestEmitter(
        gms_server=settings.datahub_gms_url,
        token=settings.datahub_gms_token or None,
    )

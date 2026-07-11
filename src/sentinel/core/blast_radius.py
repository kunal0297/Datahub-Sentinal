"""Shared lineage-walk + impact classifier used by both PR Impact Analysis
(Tier 1) and ML Blast Radius (Tier 2) — the two features that need "what
does a hop-limited walk from this asset reach, and how bad would it be if
this asset broke" are the same question asked in two directions (generic
dataset lineage vs. ML-entity lineage), so the walk and the severity-context
projection live here once.

Contract note on `DataHubClient.get_lineage` / `get_entities`: the MCP tool
NAMES are verified (see core/datahub_client.py docstring), but their exact
JSON response shapes were not — `mcp-server-datahub`'s tool list confirms
`get_lineage` and `get_entities` exist, not their payload schema. This module
therefore defines the shape it needs (`get_lineage` returns a flat list of
one-hop neighbor URN strings per call; `get_entities` returns a list of
dicts each containing at least `urn`, `type`, `name`, `owners`, `tags`,
`domain`) and `tests/conftest.py`'s `FakeDataHubClient` is the source of
truth for that contract in tests. Reconcile this against the real tool's
actual response during the deferred live-DataHub verification pass (see
ARCHITECTURE.md "Status notes") — if the real shape differs, adapt the two
`fetch_assets`/`walk_lineage` call sites, not the rest of this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from sentinel.core.incident_engine import SeverityContext
from sentinel.core.models import Asset, LineageDirection, LineageEdge, Owner, Urn

# Tags the seed data (seed/seed_datahub.py) and this module agree on. Kept as
# named constants rather than duplicated string literals so the coupling is
# visible in one place.
PRODUCTION_CRITICAL_TAG = "production-critical"
PRODUCTION_ML_TAG = "production"


class LineageBackend(Protocol):
    """The subset of DataHubClient's async MCP-backed surface this module
    needs. Duck-typed like `IncidentBackend` in core/incident_engine.py —
    `FakeDataHubClient` satisfies this without inheriting from it."""

    async def get_lineage(
        self, urn: str, direction: str = "DOWNSTREAM", hops: int = 1, **kwargs: Any
    ) -> list[str]: ...

    async def get_entities(self, urns: list[str]) -> list[dict[str, Any]]: ...


def _asset_from_entity(urn: str, entity: dict[str, Any]) -> Asset:
    owners_raw = entity.get("owners", [])
    owners = [o if isinstance(o, Owner) else Owner(urn=o) for o in owners_raw]
    tags = entity.get("tags", [])
    return Asset(
        urn=urn,
        entity_type=entity.get("type") or Urn(raw=urn).entity_type,
        name=entity.get("name", urn),
        platform=entity.get("platform"),
        owners=owners,
        tags=tags,
        domain=entity.get("domain"),
        is_production_critical=PRODUCTION_CRITICAL_TAG in tags,
        description=entity.get("description"),
    )


async def fetch_assets(client: LineageBackend, urns: list[str]) -> dict[str, Asset]:
    """Looks up each urn's Asset projection, one `get_entities` batch call
    for all of them. An urn DataHub doesn't return anything for still gets a
    minimal placeholder Asset (entity type parsed from the URN itself) rather
    than being silently dropped — PR Impact Analysis's "always report what
    you could and couldn't analyze" principle applies here too."""
    if not urns:
        return {}
    entities = await client.get_entities(urns)
    by_urn = {e["urn"]: e for e in entities if "urn" in e}
    return {
        u: _asset_from_entity(u, by_urn[u])
        if u in by_urn
        else Asset(urn=u, entity_type=Urn(raw=u).entity_type, name=u)
        for u in urns
    }


async def walk_lineage(
    client: LineageBackend,
    start_urn: str,
    direction: LineageDirection,
    hop_limit: int,
) -> list[LineageEdge]:
    """Breadth-first walk from `start_urn`, one `get_lineage(hops=1)` call
    per frontier node per hop, up to `hop_limit` hops. Stops early once a hop
    discovers nothing new. Returns every traversed edge (not just the
    frontier), so callers can reconstruct the path to any impacted asset if
    they need to (the Migration Copilot's "exact path" tracing wants this;
    PR Impact Analysis and ML Blast Radius mostly just want the reached set).
    """
    visited = {start_urn}
    frontier = [start_urn]
    edges: list[LineageEdge] = []

    for hop in range(1, hop_limit + 1):
        next_frontier: list[str] = []
        for node in frontier:
            neighbors = await client.get_lineage(node, direction=direction.value, hops=1)
            for neighbor in neighbors:
                edges.append(LineageEdge(source_urn=node, target_urn=neighbor, hops=hop))
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.append(neighbor)
        frontier = next_frontier
        if not frontier:
            break

    return edges


@dataclass
class BlastRadiusReport:
    source_urn: str
    direction: LineageDirection
    hop_limit: int
    source_asset: Asset
    impacted: list[Asset] = field(default_factory=list)
    edges: list[LineageEdge] = field(default_factory=list)

    @property
    def impacted_urns(self) -> list[str]:
        return [a.urn for a in self.impacted]

    @property
    def downstream_dataset_count(self) -> int:
        return sum(1 for a in self.impacted if a.entity_type == "dataset")

    @property
    def downstream_dashboard_count(self) -> int:
        return sum(1 for a in self.impacted if a.entity_type in ("dashboard", "chart"))

    @property
    def feeds_production_ml_model(self) -> bool:
        return any(
            a.entity_type == "mlModel" and PRODUCTION_ML_TAG in a.tags for a in self.impacted
        )

    def hops_to(self, urn: str) -> int | None:
        matches = [e.hops for e in self.edges if e.target_urn == urn]
        return min(matches) if matches else None

    def to_severity_context(self) -> SeverityContext:
        return SeverityContext(
            has_production_critical_tag=self.source_asset.is_production_critical,
            feeds_production_ml_model=self.feeds_production_ml_model,
            downstream_dashboard_count=self.downstream_dashboard_count,
            downstream_dataset_count=self.downstream_dataset_count,
        )


async def compute_blast_radius(
    client: LineageBackend,
    urn: str,
    direction: LineageDirection = LineageDirection.DOWNSTREAM,
    hop_limit: int = 3,
) -> BlastRadiusReport:
    edges = await walk_lineage(client, urn, direction, hop_limit)
    impacted_urns = sorted({e.target_urn for e in edges} - {urn})

    assets = await fetch_assets(client, [urn, *impacted_urns])
    source_asset = assets[urn]

    return BlastRadiusReport(
        source_urn=urn,
        direction=direction,
        hop_limit=hop_limit,
        source_asset=source_asset,
        impacted=[assets[u] for u in impacted_urns],
        edges=edges,
    )

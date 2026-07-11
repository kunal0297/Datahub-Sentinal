"""ML Blast Radius (Tier 2 MVP): answer "which currently-deployed models
depend, even transitively, on this asset — and is anything upstream of them
unhealthy right now?", then raise an incident on the *model entity* (not
just the table) tracing the exact path, so the person paged sees the model
at risk, not an upstream table name they've never heard of.

This reuses `core/blast_radius.walk_lineage` for the graph traversal but is
deliberately NOT the same code path as PR Impact's dataset blast radius:
ML lineage spans heterogeneous entity types (dataset -> mlFeature ->
mlFeatureTable -> mlModel) and the question here is path-shaped ("trace the
route from the unhealthy asset to the model"), not set-shaped ("what does
this reach"). `build_ml_paths` reconstructs full typed paths from the walk's
edges; nothing in PR Impact needs that.

Health is judged from two real DataHub signals, both already verified in
core/datahub_client.py: active incidents on the asset, and the latest run
result of each assertion on it (FAILURE == unhealthy).

MVP scope note (see README "Known limitations"): this is an on-demand
check — `sentinel ml-check` — plus a documented cron/Actions snippet, not a
continuously-running scheduler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from sentinel.core.blast_radius import (
    PRODUCTION_ML_TAG,
    LineageBackend,
    fetch_assets,
    walk_lineage,
)
from sentinel.core.incident_engine import IncidentEngine, SeverityContext
from sentinel.core.models import (
    Asset,
    IncidentCandidate,
    IncidentType,
    LineageDirection,
    Urn,
)

logger = logging.getLogger(__name__)

SOURCE_AGENT = "ml-blast-radius"

# ML lineage chains (dataset -> staging -> mart -> feature -> model) run
# deeper than the dataset-to-dashboard chains PR Impact walks, so this
# check defaults to a higher hop limit than Settings' generic default of 3.
DEFAULT_ML_HOP_LIMIT = 6

ML_ENTITY_TYPES = ("mlFeature", "mlFeatureTable", "mlModel", "mlModelDeployment")


class MLBlastRadiusBackend(LineageBackend, Protocol):
    """LineageBackend plus the two sync health-signal reads."""

    def get_active_incidents(self, resource_urn: str, entity_type: str) -> list[dict[str, Any]]: ...

    def get_assertions_with_latest_run(self, dataset_urn: str) -> list[dict[str, Any]]: ...


def is_production_model(asset: Asset) -> bool:
    return asset.entity_type == "mlModel" and (
        PRODUCTION_ML_TAG in asset.tags or asset.is_production_critical
    )


@dataclass
class HealthSignal:
    """One concrete reason an asset is considered unhealthy."""

    asset_urn: str
    kind: str  # "active_incident" | "failing_assertion"
    detail: str


@dataclass
class MLPath:
    """One route from the checked asset to a model, as asset projections in
    hop order (source first, model last)."""

    assets: list[Asset]

    @property
    def model(self) -> Asset:
        return self.assets[-1]

    def trace(self) -> str:
        return " -> ".join(f"{a.name} ({a.entity_type})" for a in self.assets)


def build_ml_paths(
    edges: list[tuple[str, str]], start_urn: str, assets: dict[str, Asset]
) -> list[MLPath]:
    """Reconstructs every simple path from `start_urn` to an mlModel out of
    the walk's edge list. Depth-first with a visited set per path; the walk
    already bounded the graph, so this stays small."""
    adjacency: dict[str, list[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)

    paths: list[MLPath] = []

    def _dfs(node: str, path: list[str]) -> None:
        asset = assets.get(node)
        if asset and asset.entity_type == "mlModel":
            paths.append(MLPath(assets=[assets[u] for u in path if u in assets]))
            return
        for neighbor in adjacency.get(node, []):
            if neighbor not in path:  # no cycles
                _dfs(neighbor, [*path, neighbor])

    _dfs(start_urn, [start_urn])
    return paths


def check_asset_health(client: MLBlastRadiusBackend, asset: Asset) -> list[HealthSignal]:
    """Active incidents + failing assertions for one asset. Only entity
    types with a verified incidents root query are checked for incidents,
    and only datasets carry assertions — anything else returns no signals
    rather than guessing at an unverified GraphQL field."""
    signals: list[HealthSignal] = []

    try:
        for incident in client.get_active_incidents(asset.urn, asset.entity_type):
            signals.append(
                HealthSignal(
                    asset_urn=asset.urn,
                    kind="active_incident",
                    detail=f"active incident: {incident.get('title', incident.get('urn'))}",
                )
            )
    except ValueError:
        # entity type without a verified incidents root query field
        # (e.g. mlFeature/mlFeatureTable) — skip, don't fabricate
        logger.debug("no verified incidents query for %s (%s)", asset.urn, asset.entity_type)

    if asset.entity_type == "dataset":
        for assertion in client.get_assertions_with_latest_run(asset.urn):
            if assertion.get("latest_result") == "FAILURE":
                reason = assertion.get("native_results", {}).get("reason", "")
                detail = (
                    f"failing {assertion.get('type', 'assertion')} assertion: "
                    f"{assertion.get('description') or assertion.get('urn')}"
                )
                if reason:
                    detail += f" ({reason})"
                signals.append(
                    HealthSignal(asset_urn=asset.urn, kind="failing_assertion", detail=detail)
                )

    return signals


@dataclass
class ModelRisk:
    """A production model reached from an unhealthy asset, with the exact
    path and the signals that make the upstream unhealthy."""

    model: Asset
    path: MLPath
    signals: list[HealthSignal]


@dataclass
class MLBlastRadiusReport:
    checked_urn: str
    direction: LineageDirection
    paths: list[MLPath] = field(default_factory=list)
    signals_by_asset: dict[str, list[HealthSignal]] = field(default_factory=dict)
    risks: list[ModelRisk] = field(default_factory=list)
    incidents_raised: list[str] = field(default_factory=list)

    @property
    def models_reached(self) -> list[Asset]:
        seen: dict[str, Asset] = {}
        for p in self.paths:
            seen.setdefault(p.model.urn, p.model)
        return list(seen.values())

    def to_markdown(self) -> str:
        lines = [
            "# ML Blast Radius Report",
            "",
            f"**Checked asset:** `{self.checked_urn}`",
            f"**Models reached ({self.direction.value.lower()} walk):** {len(self.models_reached)}",
            "",
        ]
        if not self.paths:
            lines.append("No ML models found within the hop limit. Nothing to check.")
            return "\n".join(lines)

        lines.append("## Dependency paths")
        for p in self.paths:
            lines.append(f"- {p.trace()}")
        lines.append("")

        lines.append("## Health signals along those paths")
        any_signal = False
        for urn, signals in self.signals_by_asset.items():
            for s in signals:
                any_signal = True
                lines.append(f"- `{urn}`: {s.detail}")
        if not any_signal:
            lines.append("- none — every asset on every path is currently healthy")
        lines.append("")

        if self.risks:
            lines.append("## ⚠ Production models at risk")
            for risk in self.risks:
                lines.append(f"### {risk.model.name}")
                lines.append(f"- Path: {risk.path.trace()}")
                for s in risk.signals:
                    lines.append(f"- Why: {s.detail} (on `{s.asset_urn}`)")
            lines.append("")
            for urn in self.incidents_raised:
                lines.append(f"Raised/updated DataHub incident on the model entity: `{urn}`")
        else:
            lines.append("No production model is downstream of an unhealthy asset. ✅")
        return "\n".join(lines)


async def run_ml_check(
    client: MLBlastRadiusBackend,
    incident_engine: IncidentEngine,
    urn: str,
    hop_limit: int = DEFAULT_ML_HOP_LIMIT,
) -> MLBlastRadiusReport:
    """The whole check. `urn` may be a dataset ("what models depend on
    this?") or an mlModel ("what does this model depend on?") — a model URN
    is checked by walking UPSTREAM to its dependencies and then evaluating
    the same dependency paths in reverse.
    """
    entity_type = Urn(raw=urn).entity_type

    if entity_type == "mlModel":
        # Walk upstream from the model, then express each path in
        # source-to-model order so the report reads the same either way.
        edges = await walk_lineage(client, urn, LineageDirection.UPSTREAM, hop_limit)
        all_urns = sorted({urn, *(e.target_urn for e in edges), *(e.source_urn for e in edges)})
        assets = await fetch_assets(client, all_urns)
        # Re-express the walk in data-flow direction (upstream asset ->
        # consumer) and rebuild paths from the deepest upstream roots only —
        # starting from every intermediate node would just emit each path's
        # suffixes again.
        reversed_edges = [(e.target_urn, e.source_urn) for e in edges]
        roots = sorted(set(all_urns) - {e.source_urn for e in edges})
        paths = []
        for root in roots:
            paths.extend(build_ml_paths(reversed_edges, root, assets))
        direction = LineageDirection.UPSTREAM
    else:
        edges = await walk_lineage(client, urn, LineageDirection.DOWNSTREAM, hop_limit)
        all_urns = sorted({urn, *(e.target_urn for e in edges)})
        assets = await fetch_assets(client, all_urns)
        paths = build_ml_paths([(e.source_urn, e.target_urn) for e in edges], urn, assets)
        direction = LineageDirection.DOWNSTREAM

    report = MLBlastRadiusReport(checked_urn=urn, direction=direction, paths=paths)

    # health-check every distinct asset that appears on some path to a model
    assets_on_paths: dict[str, Asset] = {}
    for path in paths:
        for asset in path.assets:
            assets_on_paths.setdefault(asset.urn, asset)
    for asset_urn, asset in assets_on_paths.items():
        if asset.entity_type == "mlModel":
            continue  # the model is the *subject* of the risk, not a cause
        signals = check_asset_health(client, asset)
        if signals:
            report.signals_by_asset[asset_urn] = signals

    # a production model downstream of any unhealthy asset is a risk
    for path in paths:
        if not is_production_model(path.model):
            continue
        path_signals = [s for a in path.assets for s in report.signals_by_asset.get(a.urn, [])]
        if path_signals:
            report.risks.append(ModelRisk(model=path.model, path=path, signals=path_signals))

    for risk in report.risks:
        incident = _raise_model_incident(incident_engine, risk)
        if incident.urn:
            report.incidents_raised.append(incident.urn)

    logger.info(
        "ml-check for %s: %d path(s) to %d model(s), %d at risk, %d incident(s) raised/updated",
        urn,
        len(report.paths),
        len(report.models_reached),
        len(report.risks),
        len(report.incidents_raised),
    )
    return report


def _raise_model_incident(incident_engine: IncidentEngine, risk: ModelRisk) -> Any:
    """Incident lands on the MODEL entity with the exact path in the
    description — the spec's explicit requirement, because an incident on
    `raw.orders` doesn't tell the ML on-call their fraud model is at risk."""
    freshness_driven = any(
        "FRESHNESS" in s.detail.upper() or s.kind == "failing_assertion" for s in risk.signals
    )
    incident_type = IncidentType.FRESHNESS if freshness_driven else IncidentType.OPERATIONAL
    signal_summary = "; ".join(s.detail for s in risk.signals)
    # dedup on the path + signal kinds, not the free-text detail, so the
    # same unhealthy upstream doesn't multiply incidents across re-runs
    signal_kinds = sorted({s.kind for s in risk.signals})

    candidate = IncidentCandidate(
        resource_urns=[risk.model.urn],
        incident_type=incident_type,
        source_agent=SOURCE_AGENT,
        raw_signal=f"unhealthy-upstream|{risk.path.trace()}|{signal_kinds}",
        title=f"Production model {risk.model.name} depends on an unhealthy upstream asset",
        context=(
            f"Dependency path: {risk.path.trace()}. "
            f"Unhealthy signal(s): {signal_summary}. "
            f"The model is serving production traffic; its predictions may be "
            f"degrading before any consumer-visible error appears."
        ),
    )
    severity_context = SeverityContext(
        has_production_critical_tag=risk.model.is_production_critical,
        feeds_production_ml_model=True,  # the model IS in production — see is_production_model
    )
    owner = risk.model.owners[0] if risk.model.owners else None
    return incident_engine.raise_or_update(
        candidate, severity_context, entity_type="mlModel", owner=owner
    )

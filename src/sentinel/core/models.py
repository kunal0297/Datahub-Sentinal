"""Shared types used across Sentinel's core engines and agents.

URN formats here are verified against DataHub's published metamodel docs
(docs.datahub.com/docs/generated/metamodel/entities/*) as of this writing —
see ARCHITECTURE.md "DataHub API surface" for the verification notes and
links. Do not add a new `make_*_urn` helper without checking the entity's
`*Key` aspect first; guessing URN shapes produces writes DataHub silently
rejects or misfiles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Urn(BaseModel):
    """A validated DataHub URN. Thin wrapper so callers pass typed handles
    instead of raw strings; equality/hash are string-based so URNs work as
    dict keys."""

    raw: str

    @field_validator("raw")
    @classmethod
    def _validate(cls, v: str) -> str:
        if not v.startswith("urn:li:"):
            raise ValueError(f"not a DataHub URN: {v!r}")
        return v

    @property
    def entity_type(self) -> str:
        parts = self.raw.split(":", 3)
        if len(parts) < 3:
            raise ValueError(f"malformed URN: {self.raw!r}")
        return parts[2]

    def __str__(self) -> str:
        return self.raw

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Urn):
            return self.raw == other.raw
        if isinstance(other, str):
            return self.raw == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.raw)

    model_config = {"frozen": True}


def make_dataset_urn(platform: str, name: str, env: str = "PROD") -> Urn:
    return Urn(raw=f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{env})")


def make_ml_model_urn(platform: str, name: str, env: str = "PROD") -> Urn:
    return Urn(raw=f"urn:li:mlModel:(urn:li:dataPlatform:{platform},{name},{env})")


def make_ml_feature_table_urn(platform: str, name: str) -> Urn:
    return Urn(raw=f"urn:li:mlFeatureTable:(urn:li:dataPlatform:{platform},{name})")


def make_ml_feature_urn(feature_namespace: str, name: str) -> Urn:
    return Urn(raw=f"urn:li:mlFeature:({feature_namespace},{name})")


def make_dashboard_urn(platform: str, name: str) -> Urn:
    return Urn(raw=f"urn:li:dashboard:({platform},{name})")


def make_chart_urn(platform: str, name: str) -> Urn:
    return Urn(raw=f"urn:li:chart:({platform},{name})")


def make_data_job_urn(orchestrator: str, flow_id: str, job_id: str, env: str = "PROD") -> Urn:
    return Urn(raw=f"urn:li:dataJob:(urn:li:dataFlow:({orchestrator},{flow_id},{env}),{job_id})")


class Severity(StrEnum):
    """Sentinel's own severity scale.

    Note: the verified `RaiseIncidentInput` GraphQL schema (type, customType,
    title, description, resourceUrn, source) has no native priority/severity
    field in open-source DataHub. Severity is Sentinel business logic, not a
    DataHub primitive — it is encoded as a `[SEVERITY]` prefix in the
    incident title and spelled out in the description, and used locally for
    notification routing and `block_on_critical` decisions.
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class IncidentType(StrEnum):
    """Mirrors DataHub's verified IncidentType enum values."""

    OPERATIONAL = "OPERATIONAL"
    FRESHNESS = "FRESHNESS"
    VOLUME = "VOLUME"
    COLUMN = "COLUMN"
    SQL = "SQL"
    DATA_SCHEMA = "DATA_SCHEMA"
    CUSTOM = "CUSTOM"


class IncidentState(StrEnum):
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"


class Owner(BaseModel):
    urn: str  # urn:li:corpuser:... or urn:li:corpGroup:...
    display_name: str | None = None
    is_group: bool = False


class Asset(BaseModel):
    """A DataHub entity as Sentinel's engines need to reason about it —
    a projection, not the full DataHub entity graph."""

    urn: str
    entity_type: str
    name: str
    platform: str | None = None
    owners: list[Owner] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    domain: str | None = None
    is_production_critical: bool = False
    description: str | None = None


class LineageDirection(StrEnum):
    UPSTREAM = "UPSTREAM"
    DOWNSTREAM = "DOWNSTREAM"


class LineageEdge(BaseModel):
    source_urn: str
    target_urn: str
    hops: int


class IncidentCandidate(BaseModel):
    """What a calling agent hands to the Incident Automation Engine — the
    engine decides severity, dedup, ownership, and routing from this."""

    resource_urns: list[str]
    incident_type: IncidentType
    source_agent: str
    raw_signal: str  # short machine-readable description of what tripped, used for dedup hashing
    title: str
    context: str  # human-readable "why", folded into the incident description
    link: str | None = None  # e.g. PR URL, so the incident links back to its trigger


class Incident(BaseModel):
    urn: str | None = None
    resource_urns: list[str]
    incident_type: IncidentType
    severity: Severity
    state: IncidentState = IncidentState.ACTIVE
    title: str
    description: str
    dedup_key: str
    source_agent: str
    link: str | None = None
    # Resolved by the caller (via `incident_engine.resolve_owner`, which needs
    # Asset + domain-ownership context the engine itself doesn't fetch) and
    # passed in so notifiers can address the right person.
    owner: Owner | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
    resolution_comment: str | None = None


class ProposalStatus(StrEnum):
    """Sentinel's own pending/approve/reject lifecycle.

    Verified against DataHub's GraphQL mutations doc: there is no
    propose*/accept*/reject* proposal mutation in open-source DataHub's
    public API. Sentinel therefore owns this state itself (see
    core/proposal_engine.py) and only calls a real DataHub write
    (update_description, add_tags, add_terms, updateDeprecation, ...) once a
    human accepts. This is a deliberate, documented substitution for the
    spec's assumed `accept_or_reject_proposals` MCP tool, which does not
    exist in the current tool surface (see ARCHITECTURE.md).
    """

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class MetadataChangeProposal(BaseModel):
    """A Sentinel-drafted, human-gated change to an asset's metadata."""

    id: str
    target_urn: str
    change_type: str  # "description" | "column_description" | "tags" | "terms" | "deprecation"
    field_path: str | None = None  # e.g. column name, for column-level changes
    proposed_value: str
    rationale: str  # what evidence grounded this proposal (usage stats, lineage, etc.)
    source_agent: str
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None
    decided_by: str | None = None


class ColumnChangeType(StrEnum):
    COLUMN_REMOVED = "column_removed"
    TYPE_CHANGED = "type_changed"
    COLUMN_ADDED = "column_added"
    RENAMED = "renamed"


class SchemaField(BaseModel):
    name: str
    type: str
    description: str | None = None
    nullable: bool = True


class ColumnChange(BaseModel):
    column: str
    change_type: ColumnChangeType
    breaking: bool
    detail: str
    old_type: str | None = None
    new_type: str | None = None
    renamed_to: str | None = None

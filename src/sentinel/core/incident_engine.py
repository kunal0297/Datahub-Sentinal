"""The Incident Automation Engine — shared infrastructure every agent
(PR Impact Analysis, Migration Copilot, ML Blast Radius, Quality Checker)
calls into to turn "something is wrong with this asset" into a well-formed,
deduplicated, correctly-routed DataHub incident, and to resolve it
automatically once the underlying condition clears.

Four responsibilities, each independently testable:

1. **Severity classification** (`classify_severity`) — data-driven rules
   loaded from YAML (default `config/severity_rules.yml`), not a hardcoded
   if/else chain. Severity is Sentinel-side business logic folded into the
   incident title/description; see `models.Severity` for why it isn't a
   native DataHub field.
2. **Deduplication** (`compute_dedup_key`, `IncidentEngine.raise_or_update`)
   — before creating a new incident, check DataHub for an existing *active*
   incident carrying the same dedup key (embedded as an HTML-comment marker
   in the description, since RaiseIncidentInput has no custom-metadata
   field) and update it instead of creating a duplicate.
3. **Owner resolution** (`resolve_owner`) — direct owners first, then domain
   ownership, then a configured default.
4. **Notification routing** (`IncidentEngine._notify`) — calls every
   configured `NotifierPlugin`, catching and logging failures so one broken
   notifier doesn't stop the others.

Every incident raised here states which agent raised it and why in its
description — see `_build_description` — because "quality check failed"
with no context is the actual failure mode of most observability tooling.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from sentinel.core.models import (
    Asset,
    Incident,
    IncidentCandidate,
    IncidentState,
    Owner,
    Severity,
)
from sentinel.integrations.notifiers.base import NotifierPlugin

logger = logging.getLogger(__name__)

_DEDUP_MARKER_RE = re.compile(r"<!-- sentinel:dedup_key=([0-9a-f]+) -->")


class IncidentBackend(Protocol):
    """The subset of DataHubClient's sync surface the engine needs. Both
    `DataHubClient` and `tests.conftest.FakeDataHubClient` satisfy this
    structurally — no inheritance required."""

    def raise_incident(
        self,
        resource_urn: str,
        incident_type: Any,
        title: str,
        description: str,
        custom_type: str | None = None,
    ) -> str: ...

    def update_incident_status(
        self, incident_urn: str, state: IncidentState, message: str
    ) -> bool: ...

    def get_active_incidents(self, resource_urn: str, entity_type: str) -> list[dict[str, Any]]: ...


# ------------------------------------------------------------------------ #
# Severity classification
# ------------------------------------------------------------------------ #


@dataclass
class SeverityContext:
    """The fixed set of facts severity rules are allowed to see. Extend this
    (and the operators in `_RULE_OPERATORS`) deliberately — don't let a rule
    reach into arbitrary asset/lineage internals, or the YAML config stops
    being a safe, auditable thing to hand-edit."""

    has_production_critical_tag: bool = False
    feeds_production_ml_model: bool = False
    downstream_dashboard_count: int = 0
    downstream_dataset_count: int = 0


_RULE_OPERATORS = {
    "equals": lambda actual, expected: actual == expected,
    "at_least": lambda actual, expected: actual >= expected,
}


@dataclass
class SeverityRule:
    field_name: str
    operator: str
    expected: Any
    severity: Severity

    def matches(self, context: SeverityContext) -> bool:
        actual = getattr(context, self.field_name)
        return _RULE_OPERATORS[self.operator](actual, self.expected)


@dataclass
class SeverityRules:
    rules: list[SeverityRule] = field(default_factory=list)
    default: Severity = Severity.LOW

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SeverityRules:
        rules = []
        for raw_rule in data.get("rules", []):
            when = raw_rule["when"]
            field_name = when["field"]
            if not hasattr(SeverityContext, field_name):
                raise ValueError(f"severity rule references unknown field {field_name!r}")
            operator = next((op for op in _RULE_OPERATORS if op in when), None)
            if operator is None:
                raise ValueError(f"severity rule for {field_name!r} has no recognized operator")
            rules.append(
                SeverityRule(
                    field_name=field_name,
                    operator=operator,
                    expected=when[operator],
                    severity=Severity(raw_rule["severity"]),
                )
            )
        return cls(rules=rules, default=Severity(data.get("default", "LOW")))

    @classmethod
    def from_yaml(cls, path: str | Path) -> SeverityRules:
        with open(path) as f:
            return cls.from_dict(yaml.safe_load(f))

    def classify(self, context: SeverityContext) -> Severity:
        for rule in self.rules:
            if rule.matches(context):
                return rule.severity
        return self.default


def classify_severity(context: SeverityContext, rules: SeverityRules) -> Severity:
    return rules.classify(context)


# ------------------------------------------------------------------------ #
# Dedup
# ------------------------------------------------------------------------ #


def compute_dedup_key(resource_urn: str, incident_type: Any, raw_signal: str) -> str:
    """Stable across repeated runs of the same check on the same asset with
    the same underlying signal — the whole point of dedup is that this key
    does NOT depend on wall-clock time or a run id."""
    incident_type_value = getattr(incident_type, "value", incident_type)
    digest = hashlib.sha256(
        f"{resource_urn}|{incident_type_value}|{raw_signal}".encode()
    ).hexdigest()
    return digest[:16]


def _extract_dedup_key(description: str) -> str | None:
    match = _DEDUP_MARKER_RE.search(description or "")
    return match.group(1) if match else None


# ------------------------------------------------------------------------ #
# Owner resolution
# ------------------------------------------------------------------------ #


def resolve_owner(
    asset: Asset,
    domain_owners: dict[str, list[Owner]],
    default_owner: Owner,
) -> Owner:
    """Direct owner first; then the asset's domain's owners; then the
    configured default. Never returns None — every incident needs someone
    to route to, even if that someone is a fallback triage owner."""
    if asset.owners:
        return asset.owners[0]
    if asset.domain and asset.domain in domain_owners and domain_owners[asset.domain]:
        return domain_owners[asset.domain][0]
    return default_owner


# ------------------------------------------------------------------------ #
# The engine
# ------------------------------------------------------------------------ #


class IncidentEngine:
    def __init__(
        self,
        client: IncidentBackend,
        severity_rules: SeverityRules,
        notifiers: list[NotifierPlugin] | None = None,
    ):
        self.client = client
        self.severity_rules = severity_rules
        self.notifiers = notifiers or []

    def _find_active_by_dedup_key(
        self, resource_urn: str, entity_type: str, dedup_key: str
    ) -> dict[str, Any] | None:
        for incident in self.client.get_active_incidents(resource_urn, entity_type):
            if _extract_dedup_key(incident.get("description", "")) == dedup_key:
                return incident
        return None

    def _build_description(
        self,
        candidate: IncidentCandidate,
        severity: Severity,
        dedup_key: str,
        owner: Owner | None,
    ) -> str:
        lines = [
            f"Raised by {candidate.source_agent} because: {candidate.context}",
            "",
            f"Affected resource(s): {', '.join(candidate.resource_urns)}",
        ]
        if owner:
            lines.append(f"Owner: {owner.urn}")
        if candidate.link:
            lines.append(f"Link: {candidate.link}")
        lines.append("")
        lines.append(f"<!-- sentinel:dedup_key={dedup_key} -->")
        return "\n".join(lines)

    def raise_or_update(
        self,
        candidate: IncidentCandidate,
        severity_context: SeverityContext,
        entity_type: str,
        owner: Owner | None = None,
    ) -> Incident:
        severity = classify_severity(severity_context, self.severity_rules)
        primary_urn = candidate.resource_urns[0]
        dedup_key = compute_dedup_key(primary_urn, candidate.incident_type, candidate.raw_signal)
        title = f"[{severity.value}] {candidate.title}"
        description = self._build_description(candidate, severity, dedup_key, owner)

        existing = self._find_active_by_dedup_key(primary_urn, entity_type, dedup_key)
        if existing:
            self.client.update_incident_status(
                existing["urn"],
                IncidentState.ACTIVE,
                f"Recurring signal from {candidate.source_agent}: {candidate.raw_signal}",
            )
            urn = existing["urn"]
            logger.info("deduped incident %s on %s (key=%s)", urn, primary_urn, dedup_key)
        else:
            urn = self.client.raise_incident(
                primary_urn, candidate.incident_type, title, description
            )
            logger.info("raised incident %s on %s (key=%s)", urn, primary_urn, dedup_key)

        incident = Incident(
            urn=urn,
            resource_urns=candidate.resource_urns,
            incident_type=candidate.incident_type,
            severity=severity,
            title=title,
            description=description,
            dedup_key=dedup_key,
            source_agent=candidate.source_agent,
            link=candidate.link,
            owner=owner,
        )
        self._notify(incident)
        return incident

    def resolve_if_cleared(
        self,
        resource_urn: str,
        entity_type: str,
        incident_type: Any,
        raw_signal: str,
        resolution_comment: str,
    ) -> bool:
        """Called by a check that's re-run on a schedule (e.g. Quality
        Checker) once the underlying condition it originally flagged is no
        longer true. Returns False (no-op) if there was nothing active to
        resolve, so callers can tell "already fine" apart from "just fixed
        it"."""
        dedup_key = compute_dedup_key(resource_urn, incident_type, raw_signal)
        existing = self._find_active_by_dedup_key(resource_urn, entity_type, dedup_key)
        if not existing:
            return False
        self.client.update_incident_status(
            existing["urn"], IncidentState.RESOLVED, resolution_comment
        )
        logger.info("resolved incident %s on %s (key=%s)", existing["urn"], resource_urn, dedup_key)
        return True

    def _notify(self, incident: Incident) -> None:
        for notifier in self.notifiers:
            if not notifier.is_configured():
                logger.info("notifier %s not configured, skipping", notifier.name)
                continue
            try:
                notifier.notify(incident)
            except Exception:
                logger.exception("notifier %s failed for incident %s", notifier.name, incident.urn)

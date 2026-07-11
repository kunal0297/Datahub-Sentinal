"""PR Impact Analysis orchestration: resolve changed files to DataHub URNs,
diff schemas, walk blast radius, classify severity, and render the PR
comment. `action_entrypoint.py` wires this to a real GitHub PR; tests here
exercise it against `FakeDataHubClient` and the real seed/sample_repo files.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from sentinel.agents.pr_impact.schema_diff import diff_schemas
from sentinel.core.blast_radius import BlastRadiusReport, LineageBackend, compute_blast_radius
from sentinel.core.incident_engine import IncidentEngine, SeverityContext, SeverityRules
from sentinel.core.models import (
    ColumnChange,
    Incident,
    IncidentCandidate,
    IncidentType,
    LineageDirection,
    SchemaField,
    Severity,
)

PR_COMMENT_MARKER = "<!-- sentinel:pr-impact-analysis -->"


class PRImpactBackend(LineageBackend, Protocol):
    """LineageBackend plus the one extra read the analyzer needs: the
    asset's current schema, to build the "before" side of the diff."""

    async def list_schema_fields(self, urn: str, **kwargs: Any) -> list[Any]: ...


# --------------------------------------------------------------------- #
# File -> URN resolution
# --------------------------------------------------------------------- #


@dataclass
class ResolvedFile:
    path: str
    urn: str | None
    method: str  # "manifest" | "sidecar" | "unresolved"


def _resolve_via_manifest(file_path: Path, manifest_path: Path) -> str | None:
    """Best-effort dbt manifest.json resolution. NOT exercised by the seeded
    demo (which uses the sidecar convention below) and not independently
    verified against a real dbt project or DataHub's dbt-ingestion source —
    DataHub's actual dbt URN convention varies by configuration (bare
    platform="dbt" vs. mapped onto the underlying warehouse platform via
    `target_platform`). Confirm against your instance before relying on this
    path; the sidecar convention is the one this repo's demo and tests
    actually verify."""
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    file_str = str(file_path)
    for node in manifest.get("nodes", {}).values():
        node_path = node.get("path")
        if node_path and file_str.endswith(node_path):
            schema = node.get("schema")
            alias = node.get("alias") or node.get("name")
            if schema and alias:
                return f"urn:li:dataset:(urn:li:dataPlatform:dbt,{schema}.{alias},PROD)"
    return None


def resolve_file_to_urn(file_path: Path, manifest_path: Path | None = None) -> ResolvedFile:
    """Priority order per the spec: dbt manifest.json, then a `.datahub.yml`
    sidecar next to the file, then unresolved (reported, never silently
    skipped — see `render_pr_comment`)."""
    if manifest_path is not None and manifest_path.exists():
        urn = _resolve_via_manifest(file_path, manifest_path)
        if urn:
            return ResolvedFile(path=str(file_path), urn=urn, method="manifest")

    sidecar_path = file_path.with_suffix(".datahub.yml")
    if sidecar_path.exists():
        data = yaml.safe_load(sidecar_path.read_text()) or {}
        urn = data.get("urn")
        if urn:
            return ResolvedFile(path=str(file_path), urn=urn, method="sidecar")

    return ResolvedFile(path=str(file_path), urn=None, method="unresolved")


def find_files_for_urns(repo_root: Path, urns: set[str]) -> dict[str, Path]:
    """The reverse of `resolve_file_to_urn`: given a set of URNs (e.g. the
    Migration Copilot's blast-radius consumers), scan `repo_root` for
    `.datahub.yml` sidecars and return whichever of those URNs actually map
    to a file in this repo. Only the sidecar convention is scanned — the
    manifest.json path has no reverse-lookup equivalent implemented (it
    isn't exercised by the demo either; see `_resolve_via_manifest`)."""
    found: dict[str, Path] = {}
    for sidecar in repo_root.rglob("*.datahub.yml"):
        data = yaml.safe_load(sidecar.read_text()) or {}
        urn = data.get("urn")
        if urn in urns:
            # "orders_v1.datahub.yml" -> "orders_v1.sql" (mirrors
            # resolve_file_to_urn's file_path.with_suffix(".datahub.yml"))
            source_file = sidecar.with_suffix("").with_suffix(".sql")
            if source_file.exists():
                found[urn] = source_file
    return found


# --------------------------------------------------------------------- #
# SQL column extraction (best-effort — covers plain SELECT lists with
# optional `AS` aliases and parenthesized expressions; not a general SQL
# parser, deliberately, since the demo's files are exactly this shape)
# --------------------------------------------------------------------- #

_SELECT_FROM_RE = re.compile(r"select\s+(.*?)\s+from\s", re.IGNORECASE | re.DOTALL)
_AS_RE = re.compile(r"\bas\b", re.IGNORECASE)
_BARE_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _split_top_level_commas(text: str) -> list[str]:
    parts = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def extract_select_columns(sql: str) -> list[SchemaField]:
    match = _SELECT_FROM_RE.search(sql)
    if not match:
        return []
    fields = []
    for raw in _split_top_level_commas(match.group(1)):
        expr = raw.strip()
        if not expr:
            continue
        as_parts = _AS_RE.split(expr)
        if len(as_parts) >= 2:
            name = as_parts[-1].strip().strip('"`')
        elif _BARE_IDENT_RE.match(expr):
            name = expr
        else:
            name = expr.split()[-1].strip('"`')
        fields.append(SchemaField(name=name, type=None))
    return fields


# --------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------- #


@dataclass
class FileAnalysis:
    resolved: ResolvedFile
    changes: list[ColumnChange] = field(default_factory=list)
    blast_radius: BlastRadiusReport | None = None

    @property
    def is_breaking(self) -> bool:
        return any(c.breaking for c in self.changes)


@dataclass
class PRAnalysisResult:
    files: list[FileAnalysis]
    overall_severity: Severity
    incident: Incident | None
    comment_body: str


async def _fetch_before_schema(client: PRImpactBackend, urn: str) -> list[SchemaField]:
    raw_fields = await client.list_schema_fields(urn)
    return [
        SchemaField(
            name=f["name"] if isinstance(f, dict) else f,
            type=f.get("type") if isinstance(f, dict) else None,
        )
        for f in raw_fields
    ]


def _overall_severity(file_analyses: list[FileAnalysis], severity_rules: SeverityRules) -> Severity:
    order = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]
    worst = Severity.LOW
    for fa in file_analyses:
        if not fa.is_breaking or fa.blast_radius is None:
            continue
        ctx = fa.blast_radius.to_severity_context()
        severity = severity_rules.classify(ctx)
        if order.index(severity) < order.index(worst):
            worst = severity
    return worst


def render_pr_comment(file_analyses: list[FileAnalysis], overall_severity: Severity) -> str:
    lines = [
        f"## DataHub Sentinel: PR Impact Analysis — **{overall_severity.value}**",
        "",
    ]
    any_resolved = any(fa.resolved.urn for fa in file_analyses)

    for fa in file_analyses:
        resolved = fa.resolved
        if resolved.urn is None:
            lines.append(
                f"- `{resolved.path}`: could not resolve to a DataHub asset "
                f"(no dbt manifest node and no `.datahub.yml` sidecar found) — skipped."
            )
            continue

        lines.append(f"### `{resolved.path}` → `{resolved.urn}`")
        if not fa.changes:
            lines.append("No column-level schema changes detected.")
        else:
            lines.append("")
            lines.append("| Column | Change | Breaking | Detail |")
            lines.append("|---|---|---|---|")
            for c in fa.changes:
                lines.append(
                    f"| `{c.column}` | {c.change_type.value} | "
                    f"{'⚠️ yes' if c.breaking else 'no'} | {c.detail} |"
                )

        if fa.blast_radius and fa.blast_radius.impacted:
            lines.append("")
            lines.append(
                f"**Blast radius:** {len(fa.blast_radius.impacted)} downstream asset(s) "
                f"within {fa.blast_radius.hop_limit} hop(s):"
            )
            lines.append("")
            lines.append("| Asset | Type | Owner |")
            lines.append("|---|---|---|")
            for asset in fa.blast_radius.impacted:
                owner = asset.owners[0].urn if asset.owners else "_none_"
                lines.append(f"| `{asset.urn}` | {asset.entity_type} | {owner} |")
        lines.append("")

    if not any_resolved:
        lines.append("_No changed files could be resolved to DataHub assets — nothing to analyze._")

    lines.append(PR_COMMENT_MARKER)
    return "\n".join(lines)


async def analyze_files(
    client: PRImpactBackend,
    incident_engine: IncidentEngine,
    changed_file_contents: dict[Path, str],
    severity_rules: SeverityRules,
    pr_link: str | None = None,
    hop_limit: int = 3,
    manifest_path: Path | None = None,
) -> PRAnalysisResult:
    """`changed_file_contents` maps each changed file's path to its new
    (post-PR) content — callers read this from the working tree, which
    already has the PR's changes checked out."""
    file_analyses: list[FileAnalysis] = []

    for path, content in changed_file_contents.items():
        resolved = resolve_file_to_urn(path, manifest_path)
        if resolved.urn is None:
            file_analyses.append(FileAnalysis(resolved=resolved))
            continue

        before = await _fetch_before_schema(client, resolved.urn)
        after = extract_select_columns(content)
        changes = diff_schemas(before, after)
        report = await compute_blast_radius(
            client, resolved.urn, LineageDirection.DOWNSTREAM, hop_limit
        )
        file_analyses.append(FileAnalysis(resolved=resolved, changes=changes, blast_radius=report))

    overall_severity = _overall_severity(file_analyses, severity_rules)

    incident = None
    if overall_severity in (Severity.HIGH, Severity.CRITICAL):
        breaking_file = next((fa for fa in file_analyses if fa.is_breaking), None)
        if breaking_file is not None and breaking_file.resolved.urn:
            breaking_columns = [c.column for c in breaking_file.changes if c.breaking]
            candidate = IncidentCandidate(
                resource_urns=[breaking_file.resolved.urn],
                incident_type=IncidentType.OPERATIONAL,
                source_agent="pr-impact-analysis",
                raw_signal=f"breaking_columns:{','.join(sorted(breaking_columns))}",
                title=f"Breaking schema change in {breaking_file.resolved.path}",
                context=(
                    f"columns changed: {', '.join(breaking_columns)}"
                    + (
                        f"; affects {len(breaking_file.blast_radius.impacted)} downstream asset(s)"
                        if breaking_file.blast_radius
                        else ""
                    )
                ),
                link=pr_link,
            )
            incident = incident_engine.raise_or_update(
                candidate,
                breaking_file.blast_radius.to_severity_context()
                if breaking_file.blast_radius
                else SeverityContext(),
                entity_type="dataset",
            )

    comment_body = render_pr_comment(file_analyses, overall_severity)
    return PRAnalysisResult(
        files=file_analyses,
        overall_severity=overall_severity,
        incident=incident,
        comment_body=comment_body,
    )

"""Builds the column mapping between an old and new asset's schema for the
Schema Migration Copilot: explicit mapping (human-provided) > exact name
match > fuzzy match (name similarity only — description-based tie-breaking
is deliberately not implemented yet, see the TODO below). Every inferred
mapping is returned for human review (`MigrationPlan.review_lines`) before
any code generation or PR happens — never applied blind, per the spec.

A future hook, not required now: triggering this from a Slack slash command
or a DataHub deprecation event, instead of only the `sentinel migrate` CLI
entrypoint. Left as a TODO rather than built, since nothing in Tier 1
exercises it yet.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from sentinel.core.models import SchemaField

_FUZZY_THRESHOLD = 0.55
_AMBIGUITY_MARGIN = 0.05


@dataclass
class ColumnMapping:
    old_column: str
    new_column: str
    method: str  # "explicit" | "exact" | "fuzzy"
    confidence: float  # 1.0 for explicit/exact; similarity ratio for fuzzy
    ambiguous: bool = False  # top two fuzzy candidates were too close to call


@dataclass
class MigrationPlan:
    old_urn: str
    new_urn: str
    mappings: list[ColumnMapping] = field(default_factory=list)
    unmapped_old_columns: list[str] = field(default_factory=list)  # dropped: no confident match
    unmapped_new_columns: list[str] = field(default_factory=list)  # pure additions

    def review_lines(self) -> list[str]:
        """Human-readable lines for the CLI's "print the inferred mapping
        for review" step (spec 5.2 step 2) — printed before any codegen or
        PR happens."""
        lines = []
        for m in self.mappings:
            flag = "  ** AMBIGUOUS: review carefully **" if m.ambiguous else ""
            lines.append(
                f"{m.old_column!r} -> {m.new_column!r}  "
                f"[{m.method}, confidence={m.confidence:.2f}]{flag}"
            )
        for c in self.unmapped_old_columns:
            lines.append(f"{c!r} -> NO CONFIDENT MATCH (not migrated automatically)")
        for c in self.unmapped_new_columns:
            lines.append(f"(new) {c!r} has no old counterpart -- pure addition")
        return lines

    def as_column_mapping_dict(self) -> dict[str, str]:
        """The old->new mapping in the shape schema_diff.diff_schemas and
        codegen.build_codegen_prompt need — deliberately excludes ambiguous
        and unmapped columns, since those require human judgment first."""
        return {m.old_column: m.new_column for m in self.mappings if not m.ambiguous}


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def build_column_mapping(
    old_urn: str,
    new_urn: str,
    old_schema: list[SchemaField],
    new_schema: list[SchemaField],
    explicit_mapping: dict[str, str] | None = None,
    fuzzy_threshold: float = _FUZZY_THRESHOLD,
) -> MigrationPlan:
    explicit_mapping = explicit_mapping or {}
    old_names = {f.name for f in old_schema}
    new_names = {f.name for f in new_schema}

    mappings: list[ColumnMapping] = []
    matched_old: set[str] = set()
    matched_new: set[str] = set()

    for old_name, new_name in explicit_mapping.items():
        if old_name in old_names and new_name in new_names:
            mappings.append(ColumnMapping(old_name, new_name, method="explicit", confidence=1.0))
            matched_old.add(old_name)
            matched_new.add(new_name)

    for name in sorted(old_names - matched_old):
        if name in new_names and name not in matched_new:
            mappings.append(ColumnMapping(name, name, method="exact", confidence=1.0))
            matched_old.add(name)
            matched_new.add(name)

    for old_name in sorted(old_names - matched_old):
        candidates = sorted(
            ((_similarity(old_name, new_name), new_name) for new_name in (new_names - matched_new)),
            reverse=True,
        )
        if not candidates or candidates[0][0] < fuzzy_threshold:
            continue
        best_score, best_name = candidates[0]
        ambiguous = len(candidates) > 1 and (best_score - candidates[1][0]) < _AMBIGUITY_MARGIN
        mappings.append(
            ColumnMapping(
                old_name, best_name, method="fuzzy", confidence=best_score, ambiguous=ambiguous
            )
        )
        matched_old.add(old_name)
        matched_new.add(best_name)

    return MigrationPlan(
        old_urn=old_urn,
        new_urn=new_urn,
        mappings=mappings,
        unmapped_old_columns=sorted(old_names - matched_old),
        unmapped_new_columns=sorted(new_names - matched_new),
    )

"""Quality Checking (Tier 2 MVP): "quality as code" — a small,
version-controlled YAML config (`quality_checks.yml`) declaring checks per
asset, evaluated in one of two modes:

1. **Ingestion-driven (the default demo path)** — evaluates against the
   dataset's latest ingested DataHub profile (row counts, per-column null
   proportions). Requires no warehouse credentials at all, which is exactly
   what a judge cloning this repo has. `custom_sql` checks are SKIPPED (and
   say so) in this mode, because DataHub profiles can't answer arbitrary SQL.
2. **Warehouse** — runs real SQL against a configured connection. The MVP
   ships a sqlite implementation of the `WarehouseBackend` protocol (stdlib,
   zero extra dependencies, good enough to prove the mode end-to-end);
   pointing this at Snowflake/BigQuery/Postgres means implementing the
   two-method protocol with the relevant driver — see `SqliteWarehouse`'s
   docstring TODO.

Every evaluated (non-skipped) check writes its result back to DataHub as a
**native Assertion entity + run event** (`DataHubClient.
write_assertion_result` — FIELD/VOLUME/SQL types, never the deprecated
DATASET type), so the check history is visible in the DataHub UI whether it
passed or failed. Failures additionally raise an incident through the
Incident Automation Engine; a later passing run auto-resolves it via
`resolve_if_cleared` with a comment explaining why.

Deliberately out of MVP scope (see README "Known limitations"): anomaly
detection / adaptive thresholds (that's DataHub Cloud Observe territory,
which this project intentionally doesn't depend on) and a persistent
scheduler daemon (ship a cron-friendly CLI + a scheduled-workflow example
instead).
"""

from __future__ import annotations

import logging
import operator as op_module
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, model_validator

from sentinel.core.blast_radius import compute_blast_radius
from sentinel.core.incident_engine import IncidentEngine
from sentinel.core.models import IncidentCandidate, IncidentType, LineageDirection, Urn

logger = logging.getLogger(__name__)

SOURCE_AGENT = "quality-checker"

CheckType = Literal["not_null_rate", "row_count_not_zero", "custom_sql"]

_INCIDENT_TYPE_BY_CHECK: dict[str, IncidentType] = {
    "not_null_rate": IncidentType.COLUMN,
    "row_count_not_zero": IncidentType.VOLUME,
    "custom_sql": IncidentType.SQL,
}

_EXPECT_OPERATORS = {
    "==": (op_module.eq, "EQUAL_TO"),
    "!=": (op_module.ne, "NOT_EQUAL_TO"),
    "<=": (op_module.le, "LESS_THAN_OR_EQUAL_TO"),
    ">=": (op_module.ge, "GREATER_THAN_OR_EQUAL_TO"),
    "<": (op_module.lt, "LESS_THAN"),
    ">": (op_module.gt, "GREATER_THAN"),
}


class QualityCheck(BaseModel):
    """One declared check. `table` overrides the warehouse-mode table name
    when it differs from the URN's dataset name (sqlite, for one, has no
    schema-qualified names)."""

    urn: str
    name: str
    type: CheckType
    column: str | None = None
    max_null_proportion: float = 0.05
    sql: str | None = None
    expect: str | None = None  # e.g. "== 0", "<= 5"
    table: str | None = None

    @model_validator(mode="after")
    def _validate_by_type(self) -> QualityCheck:
        Urn(raw=self.urn)
        if self.type == "not_null_rate" and not self.column:
            raise ValueError(f"check {self.name!r}: not_null_rate requires 'column'")
        if self.type == "custom_sql":
            if not self.sql or not self.expect:
                raise ValueError(f"check {self.name!r}: custom_sql requires 'sql' and 'expect'")
            parse_expect(self.expect)  # fail at config-load time, not run time
        return self

    def warehouse_table(self) -> str:
        if self.table:
            return self.table
        # urn:li:dataset:(urn:li:dataPlatform:x,NAME,ENV) -> NAME
        inner = self.urn.split("(", 1)[-1].rstrip(")")
        parts = inner.split(",")
        return parts[1] if len(parts) >= 2 else inner


def load_checks(path: str | Path) -> list[QualityCheck]:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    checks = [QualityCheck(**raw) for raw in data.get("checks", [])]
    names = [c.name for c in checks]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        # names key the assertion URN and the incident dedup key — a
        # duplicate would silently merge two different checks' histories
        raise ValueError(f"duplicate check names in {path}: {sorted(duplicates)}")
    return checks


def parse_expect(expect: str) -> tuple[str, float]:
    """`"== 0"` -> ("==", 0.0). Longest operators first so `<=` doesn't
    parse as `<`."""
    text = expect.strip()
    for symbol in sorted(_EXPECT_OPERATORS, key=len, reverse=True):
        if text.startswith(symbol):
            return symbol, float(text[len(symbol) :].strip())
    raise ValueError(f"cannot parse expect expression {expect!r} (use e.g. '== 0', '<= 5')")


class CheckStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class CheckResult:
    check: QualityCheck
    status: CheckStatus
    observed: str  # human-readable observed value ("null_proportion=0.34")
    reason: str  # why it passed/failed/was skipped
    mode: str  # "ingestion" | "warehouse"


# ------------------------------------------------------------------------ #
# Evaluation: ingestion-driven mode
# ------------------------------------------------------------------------ #


class ProfileBackend(Protocol):
    def get_latest_profile(self, dataset_urn: str) -> dict[str, Any] | None: ...


def evaluate_with_profile(check: QualityCheck, profile: dict[str, Any] | None) -> CheckResult:
    mode = "ingestion"
    if check.type == "custom_sql":
        return CheckResult(
            check,
            CheckStatus.SKIPPED,
            observed="n/a",
            reason=(
                "custom_sql checks need a warehouse connection; DataHub profiling "
                "stats can't answer arbitrary SQL. Configure warehouse mode to run this."
            ),
            mode=mode,
        )
    if profile is None:
        return CheckResult(
            check,
            CheckStatus.SKIPPED,
            observed="n/a",
            reason="no DataHub profile has been ingested for this dataset — "
            "'we don't know' is not 'it failed'",
            mode=mode,
        )

    if check.type == "row_count_not_zero":
        row_count = profile.get("rowCount")
        if row_count is None:
            return CheckResult(check, CheckStatus.SKIPPED, "n/a", "profile has no rowCount", mode)
        passed = row_count > 0
        return CheckResult(
            check,
            CheckStatus.PASSED if passed else CheckStatus.FAILED,
            observed=f"row_count={row_count}",
            reason="row count is positive" if passed else "dataset is empty",
            mode=mode,
        )

    # not_null_rate
    field_profile = (profile.get("fieldProfiles") or {}).get(check.column or "")
    if field_profile is None or field_profile.get("nullProportion") is None:
        return CheckResult(
            check,
            CheckStatus.SKIPPED,
            observed="n/a",
            reason=f"profile has no null-proportion stats for column {check.column!r}",
            mode=mode,
        )
    null_proportion = float(field_profile["nullProportion"])
    passed = null_proportion <= check.max_null_proportion
    return CheckResult(
        check,
        CheckStatus.PASSED if passed else CheckStatus.FAILED,
        observed=f"null_proportion={null_proportion:.4f}",
        reason=(
            f"null proportion {null_proportion:.4f} "
            f"{'<=' if passed else '>'} threshold {check.max_null_proportion}"
        ),
        mode=mode,
    )


# ------------------------------------------------------------------------ #
# Evaluation: warehouse mode
# ------------------------------------------------------------------------ #


class WarehouseBackend(Protocol):
    """Implement this against your real warehouse to run checks live.
    Two members; everything else in this module is engine-agnostic."""

    name: str

    def run_query(self, sql: str) -> list[tuple[Any, ...]]: ...


class SqliteWarehouse:
    """The MVP's worked warehouse implementation — stdlib sqlite3, used by
    the demo and the tests. TODO(real-warehouse): a Snowflake/Postgres/
    BigQuery implementation is this same two-member protocol backed by the
    relevant driver (snowflake-connector-python / psycopg /
    google-cloud-bigquery) plus credentials from Settings; nothing else in
    the Quality Checker changes."""

    name = "sqlite"

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def run_query(self, sql: str) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self.db_path) as conn:
            return list(conn.execute(sql).fetchall())


def evaluate_with_warehouse(check: QualityCheck, warehouse: WarehouseBackend) -> CheckResult:
    mode = "warehouse"
    table = check.warehouse_table()
    try:
        if check.type == "row_count_not_zero":
            rows = warehouse.run_query(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            row_count = int(rows[0][0])
            passed = row_count > 0
            return CheckResult(
                check,
                CheckStatus.PASSED if passed else CheckStatus.FAILED,
                observed=f"row_count={row_count}",
                reason="row count is positive" if passed else "table is empty",
                mode=mode,
            )
        if check.type == "not_null_rate":
            rows = warehouse.run_query(
                f"SELECT COUNT(*), SUM(CASE WHEN {check.column} IS NULL THEN 1 ELSE 0 END) "
                f"FROM {table}"  # noqa: S608
            )
            total, nulls = int(rows[0][0]), int(rows[0][1] or 0)
            if total == 0:
                return CheckResult(
                    check, CheckStatus.SKIPPED, "row_count=0", "table is empty", mode
                )
            null_proportion = nulls / total
            passed = null_proportion <= check.max_null_proportion
            return CheckResult(
                check,
                CheckStatus.PASSED if passed else CheckStatus.FAILED,
                observed=f"null_proportion={null_proportion:.4f}",
                reason=(
                    f"null proportion {null_proportion:.4f} "
                    f"{'<=' if passed else '>'} threshold {check.max_null_proportion}"
                ),
                mode=mode,
            )
        # custom_sql
        rows = warehouse.run_query(check.sql or "")
        observed_value = float(rows[0][0]) if rows and rows[0] else float("nan")
        symbol, expected = parse_expect(check.expect or "")
        compare, _ = _EXPECT_OPERATORS[symbol]
        passed = compare(observed_value, expected)
        return CheckResult(
            check,
            CheckStatus.PASSED if passed else CheckStatus.FAILED,
            observed=f"value={observed_value}",
            reason=f"observed {observed_value} {symbol} {expected} is {passed}",
            mode=mode,
        )
    except Exception as exc:  # a broken query must not abort the whole run
        logger.exception("warehouse evaluation failed for check %s", check.name)
        return CheckResult(
            check, CheckStatus.SKIPPED, "n/a", f"warehouse query failed: {exc}", mode
        )


# ------------------------------------------------------------------------ #
# Orchestration: evaluate -> write assertion -> raise/resolve incident
# ------------------------------------------------------------------------ #


class QualitySink(Protocol):
    """The DataHub writes the orchestrator performs — satisfied by both
    DataHubClient and FakeDataHubClient."""

    def write_assertion_result(
        self,
        dataset_urn: str,
        check_name: str,
        check_type: str,
        success: bool,
        details: dict[str, str],
        column: str | None = None,
        threshold_percentage: float | None = None,
        sql: str | None = None,
        operator: str | None = None,
        expected_value: str | None = None,
    ) -> str: ...


@dataclass
class QualityRunReport:
    mode: str
    results: list[CheckResult] = field(default_factory=list)
    assertion_urns: list[str] = field(default_factory=list)
    incidents_raised: list[str] = field(default_factory=list)
    incidents_resolved: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == CheckStatus.FAILED]

    def to_markdown(self) -> str:
        lines = [
            "# Quality Check Report",
            "",
            f"**Mode:** {self.mode}",
            f"**Checks:** {len(self.results)} — "
            f"{sum(1 for r in self.results if r.status == CheckStatus.PASSED)} passed, "
            f"{len(self.failed)} failed, "
            f"{sum(1 for r in self.results if r.status == CheckStatus.SKIPPED)} skipped",
            "",
            "| Check | Asset | Status | Observed | Why |",
            "|---|---|---|---|---|",
        ]
        for r in self.results:
            icon = {"PASSED": "✅", "FAILED": "❌", "SKIPPED": "⏭"}[r.status.value]
            lines.append(
                f"| {r.check.name} | `{r.check.urn}` | {icon} {r.status.value} "
                f"| {r.observed} | {r.reason} |"
            )
        lines.append("")
        for urn in self.incidents_raised:
            lines.append(f"Raised/updated DataHub incident: `{urn}`")
        for urn in self.incidents_resolved:
            lines.append(f"Auto-resolved DataHub incident: `{urn}` (condition cleared)")
        return "\n".join(lines)


def _quality_raw_signal(check: QualityCheck) -> str:
    """Dedup key input: stable across runs of the same check on the same
    asset, independent of the observed value — so a flapping check updates
    one incident instead of raising a new one per run, and so the passing
    run can find the incident to resolve."""
    return f"quality-check|{check.name}|{check.type}"


async def run_quality_checks(
    client: Any,
    incident_engine: IncidentEngine,
    checks: list[QualityCheck],
    mode: str,
    warehouse: WarehouseBackend | None = None,
    hop_limit: int = 3,
) -> QualityRunReport:
    """Evaluate every check, write every evaluated result back to DataHub
    as a native assertion + run event, raise incidents for failures (with
    severity driven by the asset's real blast radius), and auto-resolve
    incidents whose checks now pass.

    `client` must satisfy ProfileBackend + QualitySink + the lineage
    backend used for severity classification; `DataHubClient` and
    `FakeDataHubClient` both do."""
    if mode == "warehouse" and warehouse is None:
        raise ValueError("warehouse mode requires a WarehouseBackend")

    report = QualityRunReport(mode=mode)
    for check in checks:
        if mode == "warehouse":
            result = evaluate_with_warehouse(check, warehouse)  # type: ignore[arg-type]
        else:
            profile = client.get_latest_profile(check.urn)
            result = evaluate_with_profile(check, profile)
        report.results.append(result)
        logger.info(
            "quality check %s on %s: %s (%s)",
            check.name,
            check.urn,
            result.status.value,
            result.reason,
        )

        if result.status == CheckStatus.SKIPPED:
            continue

        # check.expect is guaranteed non-None for custom_sql by the model validator
        symbol, expected = (
            parse_expect(check.expect or "") if check.type == "custom_sql" else ("", 0.0)
        )
        assertion_urn = client.write_assertion_result(
            dataset_urn=check.urn,
            check_name=check.name,
            check_type=check.type,
            success=result.status == CheckStatus.PASSED,
            details={"observed": result.observed, "reason": result.reason, "mode": result.mode},
            column=check.column,
            threshold_percentage=check.max_null_proportion * 100,
            sql=check.sql,
            operator=_EXPECT_OPERATORS[symbol][1] if symbol else None,
            expected_value=str(expected) if check.type == "custom_sql" else None,
        )
        report.assertion_urns.append(assertion_urn)

        incident_type = _INCIDENT_TYPE_BY_CHECK[check.type]
        if result.status == CheckStatus.FAILED:
            blast = await compute_blast_radius(
                client, check.urn, LineageDirection.DOWNSTREAM, hop_limit
            )
            candidate = IncidentCandidate(
                resource_urns=[check.urn],
                incident_type=incident_type,
                source_agent=SOURCE_AGENT,
                raw_signal=_quality_raw_signal(check),
                title=f"Quality check '{check.name}' failing on {blast.source_asset.name}",
                context=(
                    f"Check '{check.name}' ({check.type}, {result.mode} mode) failed: "
                    f"{result.reason} (observed {result.observed}). "
                    f"Assertion: {assertion_urn}. "
                    f"{len(blast.impacted)} downstream asset(s) consume this dataset."
                ),
            )
            owner = blast.source_asset.owners[0] if blast.source_asset.owners else None
            incident = incident_engine.raise_or_update(
                candidate,
                blast.to_severity_context(),
                entity_type="dataset",
                owner=owner,
            )
            if incident.urn:
                report.incidents_raised.append(incident.urn)
        else:
            resolved = incident_engine.resolve_if_cleared(
                resource_urn=check.urn,
                entity_type="dataset",
                incident_type=incident_type,
                raw_signal=_quality_raw_signal(check),
                resolution_comment=(
                    f"Quality check '{check.name}' passed on re-run "
                    f"({result.mode} mode): {result.reason}. Auto-resolved by {SOURCE_AGENT}."
                ),
            )
            if resolved:
                report.incidents_resolved.append(check.urn)

    return report

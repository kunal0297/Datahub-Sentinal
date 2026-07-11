"""Unit tests for the Quality Checker. Spec DoD coverage: the YAML config
parser, BOTH evaluation modes (ingestion-driven against mocked profile
stats, warehouse against a real throwaway sqlite db), assertion write-back
on every evaluated run, incident raise on failure, and auto-resolution via
resolve_if_cleared once the underlying data is fixed.
"""

from __future__ import annotations

import sqlite3

import pytest

from sentinel.agents.quality_checker.checker import (
    CheckStatus,
    QualityCheck,
    SqliteWarehouse,
    evaluate_with_profile,
    evaluate_with_warehouse,
    load_checks,
    parse_expect,
    run_quality_checks,
)
from sentinel.core.incident_engine import IncidentEngine, SeverityRules
from sentinel.core.models import IncidentState

RAW_ORDERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.orders,PROD)"
ORDERS_V2 = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v2,PROD)"
DASHBOARD = "urn:li:dashboard:(looker,executive_orders_dashboard)"


def rules() -> SeverityRules:
    return SeverityRules.from_yaml("config/severity_rules.yml")


def not_null_check(threshold: float = 0.05) -> QualityCheck:
    return QualityCheck(
        urn=RAW_ORDERS,
        name="orders-discount-not-null",
        type="not_null_rate",
        column="discount_pct",
        max_null_proportion=threshold,
    )


def row_count_check() -> QualityCheck:
    return QualityCheck(urn=RAW_ORDERS, name="orders-row-count", type="row_count_not_zero")


def custom_sql_check() -> QualityCheck:
    return QualityCheck(
        urn=ORDERS_V2,
        name="no-negative-totals",
        type="custom_sql",
        sql="SELECT COUNT(*) FROM orders_v2 WHERE total_amount_usd < 0",
        expect="== 0",
        table="orders_v2",
    )


def seed_failing_profile(fake) -> None:
    fake.entities[RAW_ORDERS] = {
        "urn": RAW_ORDERS,
        "type": "dataset",
        "name": "raw.orders",
        "owners": ["urn:li:corpuser:bob"],
    }
    fake.entities[DASHBOARD] = {"urn": DASHBOARD, "type": "dashboard", "name": "exec"}
    fake.lineage[RAW_ORDERS] = {"DOWNSTREAM": [DASHBOARD], "UPSTREAM": []}
    fake.profiles[RAW_ORDERS] = {
        "rowCount": 48213,
        "columnCount": 3,
        "timestampMillis": 1,
        "fieldProfiles": {"discount_pct": {"nullCount": 16392, "nullProportion": 0.34}},
    }


class TestConfigParsing:
    def test_loads_valid_config(self, tmp_path):
        path = tmp_path / "quality_checks.yml"
        path.write_text(
            f"""
checks:
  - urn: "{RAW_ORDERS}"
    name: a
    type: not_null_rate
    column: discount_pct
    max_null_proportion: 0.1
  - urn: "{RAW_ORDERS}"
    name: b
    type: row_count_not_zero
  - urn: "{ORDERS_V2}"
    name: c
    type: custom_sql
    sql: "SELECT 1"
    expect: "== 1"
"""
        )
        checks = load_checks(path)
        assert [c.name for c in checks] == ["a", "b", "c"]
        assert checks[0].max_null_proportion == 0.1

    def test_not_null_rate_requires_column(self):
        with pytest.raises(ValueError, match="requires 'column'"):
            QualityCheck(urn=RAW_ORDERS, name="x", type="not_null_rate")

    def test_custom_sql_requires_sql_and_expect(self):
        with pytest.raises(ValueError, match="requires 'sql' and 'expect'"):
            QualityCheck(urn=ORDERS_V2, name="x", type="custom_sql", sql="SELECT 1")

    def test_bad_expect_fails_at_load_time(self):
        with pytest.raises(ValueError, match="cannot parse expect"):
            QualityCheck(
                urn=ORDERS_V2, name="x", type="custom_sql", sql="SELECT 1", expect="roughly 0"
            )

    def test_duplicate_names_rejected(self, tmp_path):
        path = tmp_path / "quality_checks.yml"
        path.write_text(
            f"""
checks:
  - {{urn: "{RAW_ORDERS}", name: same, type: row_count_not_zero}}
  - {{urn: "{ORDERS_V2}", name: same, type: row_count_not_zero}}
"""
        )
        with pytest.raises(ValueError, match="duplicate check names"):
            load_checks(path)

    def test_parse_expect_operators(self):
        assert parse_expect("== 0") == ("==", 0.0)
        assert parse_expect("<=5") == ("<=", 5.0)
        assert parse_expect(">= 10.5") == (">=", 10.5)

    def test_warehouse_table_derived_from_urn(self):
        assert row_count_check().warehouse_table() == "raw.orders"
        assert custom_sql_check().warehouse_table() == "orders_v2"


class TestIngestionMode:
    def test_failing_null_rate(self):
        profile = {"fieldProfiles": {"discount_pct": {"nullProportion": 0.34}}}
        result = evaluate_with_profile(not_null_check(), profile)
        assert result.status == CheckStatus.FAILED
        assert "0.3400" in result.observed

    def test_passing_null_rate(self):
        profile = {"fieldProfiles": {"discount_pct": {"nullProportion": 0.002}}}
        result = evaluate_with_profile(not_null_check(), profile)
        assert result.status == CheckStatus.PASSED

    def test_row_count(self):
        assert (
            evaluate_with_profile(row_count_check(), {"rowCount": 10}).status == CheckStatus.PASSED
        )
        assert (
            evaluate_with_profile(row_count_check(), {"rowCount": 0}).status == CheckStatus.FAILED
        )

    def test_custom_sql_is_skipped_not_failed(self):
        result = evaluate_with_profile(custom_sql_check(), {"rowCount": 10})
        assert result.status == CheckStatus.SKIPPED
        assert "warehouse" in result.reason

    def test_missing_profile_is_skipped_not_failed(self):
        result = evaluate_with_profile(not_null_check(), None)
        assert result.status == CheckStatus.SKIPPED
        assert "no DataHub profile" in result.reason

    def test_missing_column_stats_is_skipped(self):
        result = evaluate_with_profile(not_null_check(), {"fieldProfiles": {}})
        assert result.status == CheckStatus.SKIPPED


class TestWarehouseMode:
    @pytest.fixture
    def warehouse(self, tmp_path) -> SqliteWarehouse:
        db = tmp_path / "wh.db"
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE "raw.orders" (order_id TEXT, discount_pct REAL)')
            conn.executemany(
                'INSERT INTO "raw.orders" VALUES (?, ?)',
                [("1", 0.1), ("2", None), ("3", 0.2), ("4", 0.0)],
            )
            conn.execute("CREATE TABLE orders_v2 (order_id TEXT, total_amount_usd REAL)")
            conn.executemany("INSERT INTO orders_v2 VALUES (?, ?)", [("1", 10.0), ("2", -5.0)])
        return SqliteWarehouse(db)

    def test_null_rate_fail_and_pass(self, warehouse):
        check = QualityCheck(
            urn=RAW_ORDERS,
            name="n",
            type="not_null_rate",
            column="discount_pct",
            max_null_proportion=0.05,
            table='"raw.orders"',
        )
        result = evaluate_with_warehouse(check, warehouse)
        assert result.status == CheckStatus.FAILED  # 1/4 nulls = 0.25 > 0.05

        check.max_null_proportion = 0.5
        assert evaluate_with_warehouse(check, warehouse).status == CheckStatus.PASSED

    def test_row_count(self, warehouse):
        check = QualityCheck(
            urn=RAW_ORDERS, name="rc", type="row_count_not_zero", table='"raw.orders"'
        )
        assert evaluate_with_warehouse(check, warehouse).status == CheckStatus.PASSED

    def test_custom_sql_fails_on_negative_totals(self, warehouse):
        result = evaluate_with_warehouse(custom_sql_check(), warehouse)
        assert result.status == CheckStatus.FAILED  # one row has total < 0
        assert "value=1.0" in result.observed

    def test_broken_query_is_skipped_with_reason(self, warehouse):
        check = QualityCheck(
            urn=RAW_ORDERS, name="broken", type="row_count_not_zero", table="does_not_exist"
        )
        result = evaluate_with_warehouse(check, warehouse)
        assert result.status == CheckStatus.SKIPPED
        assert "warehouse query failed" in result.reason


class TestOrchestration:
    @pytest.mark.asyncio
    async def test_failure_writes_assertion_and_raises_incident(self, fake_datahub):
        seed_failing_profile(fake_datahub)
        engine = IncidentEngine(fake_datahub, rules())

        report = await run_quality_checks(
            fake_datahub, engine, [not_null_check()], mode="ingestion"
        )

        assert len(report.failed) == 1
        # native assertion written even though the check failed
        assert len(fake_datahub.assertion_results) == 1
        assert fake_datahub.assertion_results[0]["success"] is False
        # incident raised on the dataset with full audit context
        raises = [c for c in fake_datahub.calls if c[0] == "raise_incident"]
        assert len(raises) == 1
        assert raises[0][1]["resource_urn"] == RAW_ORDERS
        assert "orders-discount-not-null" in raises[0][1]["description"]
        # dashboard downstream -> HIGH severity per config/severity_rules.yml
        assert raises[0][1]["title"].startswith("[HIGH]")

    @pytest.mark.asyncio
    async def test_fix_then_rerun_auto_resolves(self, fake_datahub):
        """The spec's demo scenario: raise via a failing check, fix the
        seeded data, re-run, and the incident closes itself with a
        resolution comment explaining why."""
        seed_failing_profile(fake_datahub)
        engine = IncidentEngine(fake_datahub, rules())
        check = not_null_check()

        await run_quality_checks(fake_datahub, engine, [check], mode="ingestion")
        assert len(fake_datahub.get_active_incidents(RAW_ORDERS, "dataset")) == 1

        # "fix the data": the healed profile now passes the threshold
        fake_datahub.profiles[RAW_ORDERS]["fieldProfiles"]["discount_pct"] = {
            "nullCount": 96,
            "nullProportion": 0.002,
        }
        report = await run_quality_checks(fake_datahub, engine, [check], mode="ingestion")

        assert report.incidents_resolved == [RAW_ORDERS]
        assert fake_datahub.get_active_incidents(RAW_ORDERS, "dataset") == []
        resolved = [c for c in fake_datahub.calls if c[0] == "update_incident_status"][-1]
        assert resolved[1]["state"] == IncidentState.RESOLVED
        assert "passed on re-run" in resolved[1]["message"]
        # the passing run also wrote a SUCCESS assertion result
        assert fake_datahub.assertion_results[-1]["success"] is True

    @pytest.mark.asyncio
    async def test_skipped_checks_touch_nothing(self, fake_datahub):
        fake_datahub.entities[ORDERS_V2] = {
            "urn": ORDERS_V2,
            "type": "dataset",
            "name": "orders_v2",
        }
        engine = IncidentEngine(fake_datahub, rules())
        report = await run_quality_checks(
            fake_datahub, engine, [custom_sql_check()], mode="ingestion"
        )
        assert report.results[0].status == CheckStatus.SKIPPED
        assert fake_datahub.assertion_results == []
        assert not [c for c in fake_datahub.calls if c[0] == "raise_incident"]

    @pytest.mark.asyncio
    async def test_rerun_of_same_failure_dedups(self, fake_datahub):
        seed_failing_profile(fake_datahub)
        engine = IncidentEngine(fake_datahub, rules())
        await run_quality_checks(fake_datahub, engine, [not_null_check()], mode="ingestion")
        await run_quality_checks(fake_datahub, engine, [not_null_check()], mode="ingestion")
        raises = [c for c in fake_datahub.calls if c[0] == "raise_incident"]
        assert len(raises) == 1
        assert len(fake_datahub.get_active_incidents(RAW_ORDERS, "dataset")) == 1

    @pytest.mark.asyncio
    async def test_warehouse_mode_requires_backend(self, fake_datahub):
        engine = IncidentEngine(fake_datahub, rules())
        with pytest.raises(ValueError, match="requires a WarehouseBackend"):
            await run_quality_checks(fake_datahub, engine, [], mode="warehouse")

    @pytest.mark.asyncio
    async def test_report_markdown_shows_every_status(self, fake_datahub):
        seed_failing_profile(fake_datahub)
        fake_datahub.entities[ORDERS_V2] = {
            "urn": ORDERS_V2,
            "type": "dataset",
            "name": "orders_v2",
        }
        fake_datahub.profiles[RAW_ORDERS]["rowCount"] = 48213
        engine = IncidentEngine(fake_datahub, rules())
        report = await run_quality_checks(
            fake_datahub,
            engine,
            [not_null_check(), row_count_check(), custom_sql_check()],
            mode="ingestion",
        )
        markdown = report.to_markdown()
        assert "1 passed, 1 failed, 1 skipped" in markdown
        assert "❌ FAILED" in markdown and "✅ PASSED" in markdown and "⏭ SKIPPED" in markdown

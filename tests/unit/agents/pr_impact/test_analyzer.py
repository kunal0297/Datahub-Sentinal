from pathlib import Path

import pytest

from sentinel.agents.pr_impact.analyzer import (
    analyze_files,
    extract_select_columns,
    render_pr_comment,
    resolve_file_to_urn,
)
from sentinel.core.incident_engine import IncidentEngine, SeverityRules
from sentinel.core.models import IncidentState

SAMPLE_REPO = Path(__file__).parents[4] / "seed" / "sample_repo" / "models"
RULES_PATH = Path(__file__).parents[4] / "config" / "severity_rules.yml"

ORDERS_V1 = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v1,PROD)"
REVENUE_CHART = "urn:li:chart:(looker,orders_revenue_chart)"
EXEC_DASHBOARD = "urn:li:dashboard:(looker,executive_orders_dashboard)"
FRAUD_MODEL = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detection_v3,PROD)"


class TestResolveFileToUrn:
    def test_resolves_via_real_sidecar(self):
        resolved = resolve_file_to_urn(SAMPLE_REPO / "orders_v1.sql")
        assert resolved.method == "sidecar"
        assert resolved.urn == ORDERS_V1

    def test_unresolved_when_no_sidecar_and_no_manifest(self, tmp_path):
        orphan = tmp_path / "no_sidecar.sql"
        orphan.write_text("select 1")
        resolved = resolve_file_to_urn(orphan)
        assert resolved.method == "unresolved"
        assert resolved.urn is None

    def test_manifest_takes_priority_over_sidecar(self, tmp_path):
        import json

        model_file = tmp_path / "models" / "orders_v1.sql"
        model_file.parent.mkdir()
        model_file.write_text("select 1")
        (tmp_path / "models" / "orders_v1.datahub.yml").write_text(
            "urn: urn:li:dataset:(urn:li:dataPlatform:snowflake,from_sidecar,PROD)\n"
        )
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "nodes": {
                        "model.demo.orders_v1": {
                            "path": "orders_v1.sql",
                            "schema": "analytics",
                            "alias": "orders_v1",
                        }
                    }
                }
            )
        )
        resolved = resolve_file_to_urn(model_file, manifest_path=manifest)
        assert resolved.method == "manifest"
        assert resolved.urn == "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.orders_v1,PROD)"


class TestExtractSelectColumns:
    def test_extracts_bare_columns_from_real_orders_v1(self):
        sql = (SAMPLE_REPO / "orders_v1.sql").read_text()
        fields = extract_select_columns(sql)
        names = [f.name for f in fields]
        assert names == [
            "order_id",
            "customer_id",
            "order_date",
            "total_amount",
            "discount_pct",
            "status",
        ]

    def test_extracts_aliased_expressions_from_real_revenue_summary(self):
        sql = (SAMPLE_REPO / "customer_revenue_summary.sql").read_text()
        fields = extract_select_columns(sql)
        names = [f.name for f in fields]
        # customer_id bare, plus two computed columns with parens/functions
        # that must be matched via their AS alias, not mangled by the
        # paren-aware comma split.
        assert names == ["customer_id", "net_revenue", "completed_orders"]

    def test_no_select_returns_empty(self):
        assert extract_select_columns("-- just a comment, no query") == []


class TestRenderPrComment:
    def test_unresolved_file_is_reported_not_silently_skipped(self):
        from sentinel.agents.pr_impact.analyzer import FileAnalysis, ResolvedFile
        from sentinel.core.models import Severity

        analyses = [
            FileAnalysis(
                resolved=ResolvedFile(path="models/mystery.sql", urn=None, method="unresolved")
            )
        ]
        body = render_pr_comment(analyses, Severity.LOW)
        assert "mystery.sql" in body
        assert "could not resolve" in body

    def test_comment_always_ends_with_marker_for_idempotent_updates(self):
        from sentinel.agents.pr_impact.analyzer import PR_COMMENT_MARKER
        from sentinel.core.models import Severity

        body = render_pr_comment([], Severity.LOW)
        assert body.strip().endswith(PR_COMMENT_MARKER)


class TestAnalyzeFilesEndToEnd:
    @pytest.mark.asyncio
    async def test_breaking_change_reaching_dashboard_and_model_raises_critical_incident(
        self, fake_datahub
    ):
        """Mirrors the Definition of Done demo scenario: a PR removes a
        column feeding a dashboard (and, via the wider seed graph, a
        production model) -- Sentinel must produce a correct comment and a
        real DataHub incident."""
        fake_datahub.schema_fields = {
            ORDERS_V1: [
                {"name": "order_id", "type": "varchar"},
                {"name": "customer_id", "type": "varchar"},
                {"name": "order_date", "type": "timestamp"},
                {"name": "total_amount", "type": "numeric"},
                {"name": "discount_pct", "type": "numeric"},
                {"name": "status", "type": "varchar"},
            ]
        }
        fake_datahub.lineage = {ORDERS_V1: {"DOWNSTREAM": [REVENUE_CHART, EXEC_DASHBOARD]}}
        fake_datahub.entities = {
            ORDERS_V1: {
                "urn": ORDERS_V1,
                "type": "dataset",
                "name": "analytics.orders_v1",
                "tags": ["production-critical"],
            },
            REVENUE_CHART: {"urn": REVENUE_CHART, "type": "chart", "name": "orders_revenue_chart"},
            EXEC_DASHBOARD: {
                "urn": EXEC_DASHBOARD,
                "type": "dashboard",
                "name": "executive_orders_dashboard",
            },
        }

        # the PR's version of orders_v1.sql drops discount_pct entirely
        changed_content = {
            SAMPLE_REPO / "orders_v1.sql": (
                "select\n"
                "    order_id,\n"
                "    customer_id,\n"
                "    order_date,\n"
                "    total_amount,\n"
                "    status\n"
                "from staging.orders_cleaned\n"
            )
        }

        rules = SeverityRules.from_yaml(RULES_PATH)
        engine = IncidentEngine(fake_datahub, rules)
        result = await analyze_files(
            fake_datahub,
            engine,
            changed_content,
            rules,
            pr_link="https://github.com/acme/repo/pull/7",
        )

        assert result.overall_severity.value == "CRITICAL"
        assert result.incident is not None
        assert "discount_pct" in result.incident.description
        assert "https://github.com/acme/repo/pull/7" in result.incident.description

        active = fake_datahub.get_active_incidents(ORDERS_V1, "dataset")
        assert len(active) == 1
        assert active[0]["status"]["state"] == IncidentState.ACTIVE.value

        assert "discount_pct" in result.comment_body
        assert "column_removed" in result.comment_body
        assert REVENUE_CHART in result.comment_body

    @pytest.mark.asyncio
    async def test_unresolvable_file_does_not_crash_the_run(self, fake_datahub, tmp_path):
        orphan = tmp_path / "mystery.sql"
        orphan.write_text("select 1")
        rules = SeverityRules.from_yaml(RULES_PATH)
        engine = IncidentEngine(fake_datahub, rules)
        result = await analyze_files(fake_datahub, engine, {orphan: "select 1"}, rules)
        assert result.overall_severity.value == "LOW"
        assert result.incident is None
        assert "could not resolve" in result.comment_body

    @pytest.mark.asyncio
    async def test_safe_change_does_not_raise_incident(self, fake_datahub):
        fake_datahub.schema_fields = {ORDERS_V1: [{"name": "order_id", "type": "varchar"}]}
        fake_datahub.lineage = {}
        fake_datahub.entities = {
            ORDERS_V1: {"urn": ORDERS_V1, "type": "dataset", "name": "analytics.orders_v1"}
        }
        changed_content = {SAMPLE_REPO / "orders_v1.sql": "select order_id, new_col from x\n"}
        rules = SeverityRules.from_yaml(RULES_PATH)
        engine = IncidentEngine(fake_datahub, rules)
        result = await analyze_files(fake_datahub, engine, changed_content, rules)
        assert result.overall_severity.value == "LOW"
        assert result.incident is None

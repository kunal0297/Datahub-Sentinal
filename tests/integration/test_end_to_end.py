"""End-to-end integration test against a LIVE docker-composed DataHub.

Gated behind RUN_INTEGRATION_TESTS=1 (and skipped otherwise) so unit CI
stays hermetic. Run locally with:

    make datahub-up            # datahub docker quickstart (~8GB RAM)
    RUN_INTEGRATION_TESTS=1 pytest tests/integration -v

What this proves, per the spec's testing requirements: seeding works
against a real GMS, PR Impact Analysis produces a correct comment and a
REAL DataHub incident for a scripted breaking change, the Migration
Copilot walks real lineage and writes a real deprecation, the ML Blast
Radius traces the seeded unhealthy chain to the production model, and the
Quality Checker's fail -> heal -> auto-resolve loop changes real DataHub
state — not just that CLIs exit 0.

Tests here are deliberately synchronous and run their async agent calls
via asyncio.run, because several need to poll (DataHub's search/graph
indices lag writes by seconds) and retry loops around awaits inside a
pytest-asyncio loop are messier than owning the loop per attempt.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION_TESTS") != "1",
        reason="needs a live DataHub (set RUN_INTEGRATION_TESTS=1)",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[2]

RAW_ORDERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.orders,PROD)"
ORDERS_V1 = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v1,PROD)"
ORDERS_V2 = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v2,PROD)"
FRAUD_MODEL = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detection_v3,PROD)"


def _seed(*args: str) -> None:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "seed" / "seed_datahub.py"), *args],
        check=True,
        timeout=300,
    )


def _wait_for(condition, timeout_s: float = 120.0, interval_s: float = 5.0, what: str = ""):
    """Poll `condition` until it returns something truthy (returned), or
    fail with the last value seen. DataHub's graph/search indices lag
    writes by a few seconds — never assert immediately after a write."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = condition()
        if last:
            return last
        time.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {what}; last={last!r}")


@pytest.fixture(scope="module")
def settings():
    from sentinel.core.config import get_settings

    return get_settings()


@pytest.fixture(scope="module")
def seeded(settings):
    """Seed once for the whole module, in the default (unhealthy) state,
    then wait until the core lineage edge is queryable so individual tests
    don't each rediscover indexing lag."""
    from sentinel.core.datahub_client import DataHubClient

    _seed()

    def _lineage_indexed():
        async def _probe():
            with DataHubClient(settings) as client:
                async with client.mcp():
                    return await client.get_lineage(ORDERS_V1, direction="DOWNSTREAM", hops=1)

        try:
            return asyncio.run(_probe())
        except Exception:
            return None

    _wait_for(_lineage_indexed, what="seeded lineage to be indexed")
    return True


@pytest.fixture()
def client(settings):
    from sentinel.core.datahub_client import DataHubClient

    with DataHubClient(settings) as c:
        yield c


@pytest.fixture()
def incident_engine(client):
    from sentinel.core.incident_engine import IncidentEngine, SeverityRules

    return IncidentEngine(
        client, SeverityRules.from_yaml(REPO_ROOT / "config" / "severity_rules.yml")
    )


def test_pr_impact_breaking_change_raises_real_incident(seeded, client, incident_engine):
    """Scripted breaking PR: orders_v1.sql loses discount_pct. Expect a
    comment naming the blast radius and a real ACTIVE incident in DataHub."""
    from sentinel.agents.pr_impact.analyzer import analyze_files
    from sentinel.core.incident_engine import SeverityRules
    from sentinel.core.models import Severity

    original = (REPO_ROOT / "seed" / "sample_repo" / "models" / "orders_v1.sql").read_text()
    broken = "\n".join(line for line in original.splitlines() if "discount_pct" not in line)
    rules = SeverityRules.from_yaml(REPO_ROOT / "config" / "severity_rules.yml")

    async def _analyze():
        async with client.mcp():
            return await analyze_files(
                client,
                incident_engine,
                {REPO_ROOT / "seed" / "sample_repo" / "models" / "orders_v1.sql": broken},
                rules,
                pr_link="https://github.com/example/pr/1",
            )

    result = asyncio.run(_analyze())

    assert result.overall_severity in (Severity.HIGH, Severity.CRITICAL)
    assert "discount_pct" in result.comment_body
    assert result.incident is not None and result.incident.urn

    active = client.get_active_incidents(ORDERS_V1, "dataset")
    assert any(i["urn"] == result.incident.urn for i in active)


def test_migration_copilot_walks_real_lineage_and_deprecates(
    seeded, client, settings, tmp_path, monkeypatch
):
    """orders_v1 -> orders_v2 against a throwaway copy of the sample repo.
    The LLM step is stubbed deterministically unless ANTHROPIC_API_KEY is
    set — this test is about real DataHub lineage + the real deprecation
    write, not codegen quality (unit tests own the prompt contract)."""
    import sentinel.agents.migration_copilot.orchestrator as orch
    from sentinel.agents.migration_copilot.orchestrator import run_migration

    repo_copy = tmp_path / "sample_repo"
    shutil.copytree(REPO_ROOT / "seed" / "sample_repo", repo_copy)

    if not settings.anthropic_api_key:
        monkeypatch.setattr(
            orch,
            "generate_rewrite",
            lambda *a, **k: "-- deterministic stub rewrite\nSELECT 1\n",
        )

    async def _run():
        async with client.mcp():
            return await run_migration(client, settings, ORDERS_V1, ORDERS_V2, repo_root=repo_copy)

    result = asyncio.run(_run())

    # the mapping came from the two real schemas in DataHub
    mapping = result.plan.as_column_mapping_dict()
    assert mapping.get("discount_pct") == "discount_percentage"
    assert mapping.get("total_amount") == "total_amount_usd"
    # real downstream consumer (customer_revenue_summary) matched to a file
    assert result.records, "expected at least one consumer rewrite"
    assert (repo_copy / "migration_status.json").exists()
    # the deprecation write really happened
    assert result.deprecation_written


def test_ml_blast_radius_traces_seeded_failure_to_model(seeded, client, incident_engine):
    """The seeded failing freshness assertion on raw.orders must be traced
    across real ML lineage to fraud_detection_v3, raising an incident on
    the model entity."""
    from sentinel.agents.ml_blast_radius.checker import run_ml_check

    async def _check():
        async with client.mcp():
            return await run_ml_check(client, incident_engine, RAW_ORDERS)

    def _paths_visible():
        report = asyncio.run(_check())
        return report if report.paths else None

    report = _wait_for(_paths_visible, what="ML lineage paths from raw.orders")

    assert any(m.urn == FRAUD_MODEL for m in report.models_reached)
    assert report.risks, "seeded failing assertion should put the model at risk"
    active = client.get_active_incidents(FRAUD_MODEL, "mlModel")
    assert active, "expected a real ACTIVE incident on the model entity"
    assert "raw.orders" in (active[0].get("description") or "")


def test_quality_fail_heal_autoresolve_loop(seeded, client, incident_engine):
    """quality run (fails on seeded discount_pct) -> --heal reseed ->
    quality run again -> the incident auto-resolves in real DataHub."""
    from sentinel.agents.quality_checker.checker import load_checks, run_quality_checks

    checks = [
        c
        for c in load_checks(REPO_ROOT / "quality_checks.yml")
        if c.name == "orders-discount-not-null"
    ]
    assert checks, "demo config must contain the orders-discount-not-null check"

    async def _run():
        async with client.mcp():
            return await run_quality_checks(client, incident_engine, checks, mode="ingestion")

    report = asyncio.run(_run())
    assert report.failed, "seeded profile should fail the null-rate check"
    _wait_for(
        lambda: client.get_active_incidents(RAW_ORDERS, "dataset"),
        what="active quality incident on raw.orders",
    )

    _seed("--heal")

    def _resolved():
        second = asyncio.run(_run())
        return second if (not second.failed and second.incidents_resolved) else None

    report2 = _wait_for(_resolved, what="healed profile to index and incident to auto-resolve")
    assert report2.incidents_resolved == [RAW_ORDERS]
    assert client.get_active_incidents(RAW_ORDERS, "dataset") == []

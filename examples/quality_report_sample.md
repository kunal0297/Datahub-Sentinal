<!--
Real, generated (not hand-written) output of `sentinel.agents.quality_checker.checker.run_quality_checks(...).to_markdown()` in ingestion-driven mode (no warehouse credentials), against a FakeDataHubClient seeded to mirror seed/seed_datahub.py's default state: discount_pct's 34% null rate fails, row count passes, and the custom_sql check is SKIPPED with the reason ingestion mode can't run it. This is what `sentinel quality run` prints.
-->

# Quality Check Report

**Mode:** ingestion
**Checks:** 3 — 1 passed, 1 failed, 1 skipped

| Check | Asset | Status | Observed | Why |
|---|---|---|---|---|
| orders-discount-not-null | `urn:li:dataset:(urn:li:dataPlatform:postgres,raw.orders,PROD)` | ❌ FAILED | null_proportion=0.3400 | null proportion 0.3400 > threshold 0.05 |
| orders-row-count | `urn:li:dataset:(urn:li:dataPlatform:postgres,raw.orders,PROD)` | ✅ PASSED | row_count=48213 | row count is positive |
| orders-v2-no-negative-totals | `urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v2,PROD)` | ⏭ SKIPPED | n/a | custom_sql checks need a warehouse connection; DataHub profiling stats can't answer arbitrary SQL. Configure warehouse mode to run this. |

Raised/updated DataHub incident: `urn:li:incident:fake-1`

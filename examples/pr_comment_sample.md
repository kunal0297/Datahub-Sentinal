<!--
This is a real, generated (not hand-written) output of
`sentinel.agents.pr_impact.analyzer.render_pr_comment`, produced by running
that function against a FakeDataHubClient seeded to mirror the real
seed/seed_datahub.py graph, for a PR whose diff drops the `discount_pct`
column from `models/orders_v1.sql`. This is exactly the Markdown body
Sentinel posts (and idempotently updates) as a PR comment via
`GitHubClient.upsert_pr_comment` — see agents/pr_impact/analyzer.py and
agents/pr_impact/github_client.py.
-->

## DataHub Sentinel: PR Impact Analysis — **CRITICAL**

### `models/orders_v1.sql` → `urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v1,PROD)`

| Column | Change | Breaking | Detail |
|---|---|---|---|
| `discount_pct` | column_removed | ⚠️ yes | column 'discount_pct' removed |

**Blast radius:** 3 downstream asset(s) within 3 hop(s):

| Asset | Type | Owner |
|---|---|---|
| `urn:li:chart:(looker,orders_revenue_chart)` | chart | urn:li:corpuser:carol |
| `urn:li:dashboard:(looker,executive_orders_dashboard)` | dashboard | urn:li:corpuser:carol |
| `urn:li:dashboard:(looker,regional_sales_dashboard)` | dashboard | urn:li:corpuser:carol |

<!-- sentinel:pr-impact-analysis -->

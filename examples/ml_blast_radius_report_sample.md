<!--
Real, generated (not hand-written) output of `sentinel.agents.ml_blast_radius.checker.run_ml_check(...).to_markdown()`, produced against a FakeDataHubClient seeded to mirror seed/seed_datahub.py's graph in its default UNHEALTHY state (raw.orders' freshness assertion failing). This is what `sentinel ml-check --urn <raw.orders>` prints; the incident it references is raised on the mlModel entity in DataHub.
-->

# ML Blast Radius Report

**Checked asset:** `urn:li:dataset:(urn:li:dataPlatform:postgres,raw.orders,PROD)`
**Models reached (downstream walk):** 1

## Dependency paths
- raw.orders (dataset) -> staging.orders_cleaned (dataset) -> analytics.orders_v2 (dataset) -> ltv_30d (mlFeature) -> customer_ltv_features (mlFeatureTable) -> fraud_detection_v3 (mlModel)

## Health signals along those paths
- `urn:li:dataset:(urn:li:dataPlatform:postgres,raw.orders,PROD)`: failing FRESHNESS assertion: raw.orders must land by 6 AM UTC daily. (no new rows landed since yesterday 23:10 UTC)

## ⚠ Production models at risk
### fraud_detection_v3
- Path: raw.orders (dataset) -> staging.orders_cleaned (dataset) -> analytics.orders_v2 (dataset) -> ltv_30d (mlFeature) -> customer_ltv_features (mlFeatureTable) -> fraud_detection_v3 (mlModel)
- Why: failing FRESHNESS assertion: raw.orders must land by 6 AM UTC daily. (no new rows landed since yesterday 23:10 UTC) (on `urn:li:dataset:(urn:li:dataPlatform:postgres,raw.orders,PROD)`)

Raised/updated DataHub incident on the model entity: `urn:li:incident:fake-1`

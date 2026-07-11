#!/usr/bin/env python
"""Populates a fresh DataHub OSS instance with a small, coherent synthetic
e-commerce + ML dataset so every Tier 1/2 Sentinel feature has something real
to operate on. Run via `make seed` (after `make datahub-up`), or directly:

    python seed/seed_datahub.py

All aspect/class names and constructor signatures here were confirmed by
introspecting the installed `acryl-datahub` package (`inspect.signature`)
rather than assumed from memory — see ARCHITECTURE.md for the equivalent
notes on the GraphQL/MCP surface. This script uses the low-level
MetadataChangeProposalWrapper + DatahubRestEmitter pattern (not the newer
high-level `datahub.sdk.DataHubClient` entity API), because that pattern is
what DataHub's own official example library
(metadata-ingestion/examples/library/*.py) actually uses for exactly this
kind of bulk, multi-aspect entity emission — see e.g. `mlfeature_create.py`
and `assertion_create_freshness.py`.

Graph built (see README/Section 10 for the full narrative):

    raw.orders ──┐
    raw.payments ┴─> staging.orders_cleaned ─> analytics.orders_v1 (deprecated soon)
                                              └─> analytics.orders_v2 (replacement)
    raw.customers ───────────────────────────────────┘ (referenced, not transformed)

    analytics.orders_v1 ─> executive_orders_dashboard (dashboard)
                        ─> regional_sales_dashboard (dashboard)
                        ─> orders_revenue_chart (chart) ─> executive_orders_dashboard

    analytics.orders_v2 ─> features.customer_ltv_features (mlFeatureTable)
                                  └─ customer.ltv_30d (mlFeature)
                                        └─> fraud_detection_v3 (mlModel, tagged production)

    raw.orders has a FAILING freshness assertion — the ML Blast Radius check
    (Tier 2) should trace this all the way to fraud_detection_v3.
"""

from __future__ import annotations

import os
import time

import datahub.emitter.mce_builder as builder
import datahub.metadata.schema_classes as models
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter

GMS_URL = os.getenv("DATAHUB_GMS_URL", "http://localhost:8080")
GMS_TOKEN = os.getenv("DATAHUB_GMS_TOKEN") or None
SEED_ACTOR = "urn:li:corpuser:sentinel-seed"


def now_millis() -> int:
    return int(time.time() * 1000)


def audit_stamp() -> models.AuditStampClass:
    return models.AuditStampClass(time=now_millis(), actor=SEED_ACTOR)


class Seeder:
    def __init__(self, emitter: DatahubRestEmitter):
        self.emitter = emitter
        self.emitted = 0

    def emit(self, urn: str, aspect) -> None:
        self.emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect))
        self.emitted += 1

    # -- reusable builders -------------------------------------------------

    def field(
        self, path: str, type_cls, native_type: str, description: str, nullable: bool = True
    ) -> models.SchemaFieldClass:
        return models.SchemaFieldClass(
            fieldPath=path,
            type=models.SchemaFieldDataTypeClass(type=type_cls()),
            nativeDataType=native_type,
            description=description,
            nullable=nullable,
        )

    def dataset(
        self,
        urn: str,
        name: str,
        description: str,
        fields: list[models.SchemaFieldClass],
        platform: str,
        owners: list[tuple[str, models.OwnershipTypeClass]] | None = None,
        tags: list[str] | None = None,
        domain: str | None = None,
    ) -> None:
        self.emit(urn, models.DatasetPropertiesClass(name=name, description=description))
        self.emit(
            urn,
            models.SchemaMetadataClass(
                schemaName=name,
                platform=builder.make_data_platform_urn(platform),
                version=0,
                hash="",
                platformSchema=models.OtherSchemaClass(rawSchema=""),
                fields=fields,
            ),
        )
        if owners:
            self.emit(
                urn,
                models.OwnershipClass(
                    owners=[models.OwnerClass(owner=o, type=t) for o, t in owners]
                ),
            )
        if tags:
            self.emit(
                urn,
                models.GlobalTagsClass(
                    tags=[models.TagAssociationClass(tag=builder.make_tag_urn(t)) for t in tags]
                ),
            )
        if domain:
            self.emit(urn, models.DomainsClass(domains=[builder.make_domain_urn(domain)]))

    def lineage(self, downstream_urn: str, upstream_urns: list[str]) -> None:
        self.emit(
            downstream_urn,
            models.UpstreamLineageClass(
                upstreams=[
                    models.UpstreamClass(dataset=u, type=models.DatasetLineageTypeClass.TRANSFORMED)
                    for u in upstream_urns
                ]
            ),
        )

    def corp_user(self, urn: str, full_name: str, email: str) -> None:
        self.emit(
            urn,
            models.CorpUserInfoClass(
                active=True, displayName=full_name, email=email, fullName=full_name
            ),
        )

    def domain(self, id_: str, name: str, description: str) -> str:
        urn = builder.make_domain_urn(id_)
        self.emit(urn, models.DomainPropertiesClass(name=name, description=description))
        return urn


def main() -> None:
    emitter = DatahubRestEmitter(gms_server=GMS_URL, token=GMS_TOKEN)
    emitter.test_connection()
    s = Seeder(emitter)

    # -- people -------------------------------------------------------------
    alice = builder.make_user_urn("alice")
    bob = builder.make_user_urn("bob")
    carol = builder.make_user_urn("carol")
    s.corp_user(alice, "Alice Nguyen", "alice@sentinel-demo.example")
    s.corp_user(bob, "Bob Ibarra", "bob@sentinel-demo.example")
    s.corp_user(carol, "Carol Whitfield", "carol@sentinel-demo.example")

    # -- domain ---------------------------------------------------------------
    s.domain("Commerce", "Commerce", "Order lifecycle, payments, and revenue reporting.")

    # -- raw layer ------------------------------------------------------------
    raw_orders = builder.make_dataset_urn("postgres", "raw.orders", "PROD")
    s.dataset(
        raw_orders,
        "raw.orders",
        "Raw OLTP orders table, replicated from the checkout service.",
        [
            s.field("order_id", models.StringTypeClass, "varchar", "Primary key.", nullable=False),
            s.field("customer_id", models.StringTypeClass, "varchar", "FK to raw.customers."),
            s.field("order_date", models.DateTypeClass, "timestamp", "When the order was placed."),
            s.field(
                "amount", models.NumberTypeClass, "numeric", "Order total in USD, pre-discount."
            ),
            s.field("discount_pct", models.NumberTypeClass, "numeric", "Discount applied, 0-1."),
            s.field("status", models.StringTypeClass, "varchar", "Order status enum."),
        ],
        platform="postgres",
        owners=[(bob, models.OwnershipTypeClass.TECHNICAL_OWNER)],
        domain="Commerce",
    )

    raw_customers = builder.make_dataset_urn("postgres", "raw.customers", "PROD")
    s.dataset(
        raw_customers,
        "raw.customers",
        "Raw OLTP customers table.",
        [
            s.field(
                "customer_id", models.StringTypeClass, "varchar", "Primary key.", nullable=False
            ),
            s.field("email", models.StringTypeClass, "varchar", "Customer email."),
            s.field("signup_date", models.DateTypeClass, "timestamp", "Account creation date."),
        ],
        platform="postgres",
        owners=[(bob, models.OwnershipTypeClass.TECHNICAL_OWNER)],
        domain="Commerce",
    )

    raw_payments = builder.make_dataset_urn("postgres", "raw.payments", "PROD")
    s.dataset(
        raw_payments,
        "raw.payments",
        "Raw OLTP payments table, one row per payment attempt.",
        [
            s.field(
                "payment_id", models.StringTypeClass, "varchar", "Primary key.", nullable=False
            ),
            s.field("order_id", models.StringTypeClass, "varchar", "FK to raw.orders."),
            s.field("amount", models.NumberTypeClass, "numeric", "Amount charged in USD."),
            s.field("method", models.StringTypeClass, "varchar", "Payment method."),
        ],
        platform="postgres",
        # deliberately ownerless — exercises the Incident Automation Engine's
        # domain/default-owner fallback (see core/incident_engine.py Phase 1).
        domain="Commerce",
    )

    # -- staging --------------------------------------------------------------
    staging_orders_cleaned = builder.make_dataset_urn("snowflake", "staging.orders_cleaned", "PROD")
    s.dataset(
        staging_orders_cleaned,
        "staging.orders_cleaned",
        "dbt staging model: raw.orders joined with raw.payments, deduped and typed.",
        [
            s.field("order_id", models.StringTypeClass, "varchar", "Primary key.", nullable=False),
            s.field("customer_id", models.StringTypeClass, "varchar", "FK to raw.customers."),
            s.field("order_date", models.DateTypeClass, "timestamp", "When the order was placed."),
            s.field(
                "amount", models.NumberTypeClass, "numeric", "Order total in USD, pre-discount."
            ),
            s.field("discount_pct", models.NumberTypeClass, "numeric", "Discount applied, 0-1."),
            s.field("status", models.StringTypeClass, "varchar", "Order status enum."),
        ],
        platform="snowflake",
        # deliberately ownerless (see raw.payments note above) — still in a
        # domain, so owner resolution should fall back to domain/default
        # rather than finding a direct owner.
        domain="Commerce",
    )
    s.lineage(staging_orders_cleaned, [raw_orders, raw_payments])

    # -- analytics.orders_v1 (soon to be deprecated) ---------------------------
    orders_v1 = builder.make_dataset_urn("snowflake", "analytics.orders_v1", "PROD")
    s.dataset(
        orders_v1,
        "analytics.orders_v1",
        "Canonical orders mart. Powers exec/regional dashboards. Being replaced by orders_v2.",
        [
            s.field("order_id", models.StringTypeClass, "varchar", "Primary key.", nullable=False),
            s.field("customer_id", models.StringTypeClass, "varchar", "FK to raw.customers."),
            s.field("order_date", models.DateTypeClass, "timestamp", "When the order was placed."),
            s.field(
                "total_amount",
                models.NumberTypeClass,
                "numeric",
                "Order total in USD, pre-discount.",
            ),
            s.field("discount_pct", models.NumberTypeClass, "numeric", "Discount applied, 0-1."),
            s.field("status", models.StringTypeClass, "varchar", "Order status enum."),
        ],
        platform="snowflake",
        owners=[(alice, models.OwnershipTypeClass.DATAOWNER)],
        tags=["production-critical"],
        domain="Commerce",
    )
    s.lineage(orders_v1, [staging_orders_cleaned])

    # -- downstream dataset consumer (maps to seed/sample_repo customer_revenue_summary.sql) --
    revenue_summary = builder.make_dataset_urn(
        "snowflake", "analytics.customer_revenue_summary", "PROD"
    )
    s.dataset(
        revenue_summary,
        "analytics.customer_revenue_summary",
        "Per-customer net revenue rollup, built on analytics.orders_v1. A real repo file "
        "backs this (seed/sample_repo/models/customer_revenue_summary.sql) so PR Impact "
        "Analysis and the Migration Copilot have a genuine downstream consumer to find.",
        [
            s.field("customer_id", models.StringTypeClass, "varchar", "FK to raw.customers."),
            s.field("net_revenue", models.NumberTypeClass, "numeric", "Post-discount revenue."),
            s.field(
                "completed_orders", models.NumberTypeClass, "bigint", "Count of completed orders."
            ),
        ],
        platform="snowflake",
        owners=[(carol, models.OwnershipTypeClass.BUSINESS_OWNER)],
        domain="Commerce",
    )
    s.lineage(revenue_summary, [orders_v1])

    # -- analytics.orders_v2 (replacement — renamed/retyped/added columns) -----
    orders_v2 = builder.make_dataset_urn("snowflake", "analytics.orders_v2", "PROD")
    s.dataset(
        orders_v2,
        "analytics.orders_v2",
        "Replacement orders mart: splits currency out of total_amount, clarifies column names.",
        [
            s.field("order_id", models.StringTypeClass, "varchar", "Primary key.", nullable=False),
            s.field("customer_id", models.StringTypeClass, "varchar", "FK to raw.customers."),
            s.field("order_date", models.DateTypeClass, "timestamp", "When the order was placed."),
            # renamed from total_amount + retyped string->number is unchanged (number->number,
            # but semantically rescoped to be currency-agnostic — see currency field below)
            s.field(
                "total_amount_usd",
                models.NumberTypeClass,
                "numeric",
                "Order total in USD, pre-discount.",
            ),
            # renamed from discount_pct
            s.field(
                "discount_percentage", models.NumberTypeClass, "numeric", "Discount applied, 0-100."
            ),
            # renamed from status
            s.field("order_status", models.StringTypeClass, "varchar", "Order status enum."),
            # new column
            s.field("currency", models.StringTypeClass, "varchar", "ISO 4217 currency code."),
        ],
        platform="snowflake",
        owners=[(alice, models.OwnershipTypeClass.DATAOWNER)],
        domain="Commerce",
    )
    s.lineage(orders_v2, [staging_orders_cleaned])

    # -- downstream BI assets on orders_v1 --------------------------------------
    revenue_chart = builder.make_chart_urn("looker", "orders_revenue_chart")
    s.emit(
        revenue_chart,
        models.ChartInfoClass(
            title="Orders Revenue",
            description="Daily revenue trend from analytics.orders_v1.",
            lastModified=models.ChangeAuditStampsClass(lastModified=audit_stamp()),
            inputs=[orders_v1],
        ),
    )
    s.emit(
        revenue_chart,
        models.OwnershipClass(
            owners=[models.OwnerClass(owner=carol, type=models.OwnershipTypeClass.BUSINESS_OWNER)]
        ),
    )

    exec_dashboard = builder.make_dashboard_urn("looker", "executive_orders_dashboard")
    s.emit(
        exec_dashboard,
        models.DashboardInfoClass(
            title="Executive Orders Dashboard",
            description="Company-wide orders and revenue overview for leadership.",
            lastModified=models.ChangeAuditStampsClass(lastModified=audit_stamp()),
            datasets=[orders_v1],
            charts=[revenue_chart],
        ),
    )
    s.emit(
        exec_dashboard,
        models.OwnershipClass(
            owners=[models.OwnerClass(owner=carol, type=models.OwnershipTypeClass.BUSINESS_OWNER)]
        ),
    )
    s.emit(
        exec_dashboard,
        models.GlobalTagsClass(
            tags=[models.TagAssociationClass(tag=builder.make_tag_urn("production-critical"))]
        ),
    )

    regional_dashboard = builder.make_dashboard_urn("looker", "regional_sales_dashboard")
    s.emit(
        regional_dashboard,
        models.DashboardInfoClass(
            title="Regional Sales Dashboard",
            description="Orders broken out by region, used by regional sales leads.",
            lastModified=models.ChangeAuditStampsClass(lastModified=audit_stamp()),
            datasets=[orders_v1],
        ),
    )
    s.emit(
        regional_dashboard,
        models.OwnershipClass(
            owners=[models.OwnerClass(owner=carol, type=models.OwnershipTypeClass.BUSINESS_OWNER)]
        ),
    )

    # -- ML chain: orders_v2 -> feature table -> feature -> model --------------
    feature_table_urn = builder.make_ml_feature_table_urn("feast", "customer_ltv_features")
    ltv_feature_urn = builder.make_ml_feature_urn("customer_ltv_features", "ltv_30d")

    s.emit(
        ltv_feature_urn,
        models.MLFeaturePropertiesClass(
            description="Customer lifetime value, trailing 30 days, derived from orders_v2.",
            dataType="CONTINUOUS",
            sources=[orders_v2],
        ),
    )
    s.emit(
        feature_table_urn,
        models.MLFeatureTablePropertiesClass(
            description="Customer LTV feature set used by fraud and churn models.",
            mlFeatures=[ltv_feature_urn],
        ),
    )
    s.emit(
        feature_table_urn,
        models.OwnershipClass(
            owners=[models.OwnerClass(owner=alice, type=models.OwnershipTypeClass.DATAOWNER)]
        ),
    )

    fraud_model_urn = builder.make_ml_model_urn("sagemaker", "fraud_detection_v3", "PROD")
    s.emit(
        fraud_model_urn,
        models.MLModelPropertiesClass(
            description="Gradient-boosted fraud classifier, currently serving production traffic.",
            type="classification",
            mlFeatures=[ltv_feature_urn],
            customProperties={"deployment_status": "PRODUCTION"},
        ),
    )
    s.emit(
        fraud_model_urn,
        models.GlobalTagsClass(
            tags=[models.TagAssociationClass(tag=builder.make_tag_urn("production"))]
        ),
    )
    s.emit(
        fraud_model_urn,
        models.OwnershipClass(
            owners=[models.OwnerClass(owner=bob, type=models.OwnershipTypeClass.DATAOWNER)]
        ),
    )

    # -- failing freshness assertion on raw.orders (feeds the ML chain) -------
    assertion_urn = builder.make_assertion_urn(
        builder.datahub_guid({"entity": raw_orders, "type": "freshness-daily-6am"})
    )
    s.emit(
        assertion_urn,
        models.AssertionInfoClass(
            type=models.AssertionTypeClass.FRESHNESS,
            description="raw.orders must land by 6 AM UTC daily.",
            freshnessAssertion=models.FreshnessAssertionInfoClass(
                type=models.FreshnessAssertionTypeClass.DATASET_CHANGE,
                entity=raw_orders,
                schedule=models.FreshnessAssertionScheduleClass(
                    type=models.FreshnessAssertionScheduleTypeClass.CRON,
                    cron=models.FreshnessCronScheduleClass(cron="0 6 * * *", timezone="UTC"),
                ),
            ),
        ),
    )
    s.emit(
        assertion_urn,
        models.AssertionRunEventClass(
            timestampMillis=now_millis(),
            runId="seed-run-1",
            asserteeUrn=raw_orders,
            status=models.AssertionRunStatusClass.COMPLETE,
            assertionUrn=assertion_urn,
            result=models.AssertionResultClass(
                type=models.AssertionResultTypeClass.FAILURE,
                nativeResults={"reason": "no new rows landed since yesterday 23:10 UTC"},
            ),
        ),
    )

    print(f"Seeded {s.emitted} aspects against {GMS_URL}")
    print(f"  raw.orders            = {raw_orders}")
    print(f"  raw.customers         = {raw_customers}")
    print(f"  raw.payments          = {raw_payments} (deliberately ownerless)")
    print(f"  staging.orders_cleaned= {staging_orders_cleaned} (deliberately ownerless)")
    print(f"  analytics.orders_v1   = {orders_v1}")
    print(f"  analytics.orders_v2   = {orders_v2}")
    print(f"  fraud_detection_v3    = {fraud_model_urn}")
    print(f"  failing assertion     = {assertion_urn}")


if __name__ == "__main__":
    main()

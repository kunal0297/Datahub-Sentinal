-- Replacement orders mart. Mirrors analytics.orders_v2 in the seeded
-- DataHub instance — see seed/seed_datahub.py. Column renames vs
-- orders_v1: total_amount -> total_amount_usd, discount_pct ->
-- discount_percentage, status -> order_status; adds a new `currency` column.

select
    order_id,
    customer_id,
    order_date,
    total_amount_usd,
    discount_percentage,
    order_status,
    currency
from {{ ref('orders_cleaned') }}

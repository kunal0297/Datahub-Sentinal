-- Downstream consumer of orders_v1. This is the file the Schema Migration
-- Copilot (Tier 1) should find via lineage + file-to-URN resolution and
-- rewrite when migrating orders_v1 -> orders_v2 (total_amount ->
-- total_amount_usd, discount_pct -> discount_percentage, status ->
-- order_status). It is also the file PR Impact Analysis's blast-radius walk
-- should surface when a breaking change lands in orders_v1.

select
    customer_id,
    sum(total_amount * (1 - discount_pct)) as net_revenue,
    count(*) filter (where status = 'completed') as completed_orders
from {{ ref('orders_v1') }}
group by customer_id

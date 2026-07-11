-- Canonical orders mart. Mirrors analytics.orders_v1 in the seeded DataHub
-- instance — see seed/seed_datahub.py. This file exists so PR Impact
-- Analysis and the Migration Copilot (Tier 1) have a real file to resolve
-- to a DataHub URN and diff against, without needing a real dbt project or
-- warehouse. See orders_v1.datahub.yml for the URN mapping convention.

select
    order_id,
    customer_id,
    order_date,
    total_amount,
    discount_pct,
    status
from {{ ref('orders_cleaned') }}

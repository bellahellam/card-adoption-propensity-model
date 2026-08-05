-- Features must never be generated from a transaction date later than the
-- latest complete day available to the weekly pipeline.
select *
from {{ ref('gold_customer_features') }}
where feature_date > current_date - interval 1 day


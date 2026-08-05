{{
  config(
    materialized='external',
    format='parquet',
    location=s3_partitioned_location('features', 'features_transactional')
  )
}}

select
    *,
    case
        when txn_count_30d >= 20 and digital_ratio_30d >= 0.7 then 'power_digital'
        when txn_count_30d >= 10 and digital_ratio_30d >= 0.4 then 'active_digital'
        when txn_count_30d >= 5 then 'casual'
        else 'dormant'
    end as engagement_segment
from {{ ref('gold_customer_features') }}
-- run_date defaults to dbt's current run date, so this is CURRENT_DATE - 1
-- in scheduled runs while allowing idempotent backfills with --vars run_date.
where feature_date = cast('{{ var("run_date", run_started_at.strftime("%Y-%m-%d")) }}' as date) - interval 1 day

{{
  config(
    materialized='external',
    format='parquet',
    location=s3_partitioned_location('gold', 'gold_customer_features')
  )
}}

with daily_customer_transactions as (
    select
        customer_token,
        transaction_date as feature_date,
        count(*) as daily_txn_count,
        sum(amount_usd) as daily_volume_usd,
        sum(case when channel_clean = 'digital' then 1 else 0 end) as daily_digital_count,
        sum(case when is_cross_border then 1 else 0 end) as daily_cross_border_count,
        avg(amount_usd) as daily_avg_txn_size,
        max(age) as age,
        max(tenure_months) as tenure_months
    from {{ ref('silver_transactions') }}
    group by 1, 2
),
rolling_features as (
    select
        customer_token,
        feature_date,
        {{ rolling_window('daily_txn_count', 'customer_token', 'feature_date', 7) }} as txn_count_7d,
        {{ rolling_window('daily_txn_count', 'customer_token', 'feature_date', 30) }} as txn_count_30d,
        {{ rolling_window('daily_txn_count', 'customer_token', 'feature_date', 90) }} as txn_count_90d,
        {{ rolling_window('daily_volume_usd', 'customer_token', 'feature_date', 7) }} as volume_7d,
        {{ rolling_window('daily_volume_usd', 'customer_token', 'feature_date', 30) }} as volume_30d,
        {{ rolling_window('daily_volume_usd', 'customer_token', 'feature_date', 90) }} as volume_90d,
        sum(daily_volume_usd) over (
            partition by customer_token order by feature_date rows between 29 preceding and current row
        ) / nullif(sum(daily_txn_count) over (
            partition by customer_token order by feature_date rows between 29 preceding and current row
        ), 0) as avg_txn_size_30d,
        sum(daily_digital_count) over (
            partition by customer_token order by feature_date rows between 29 preceding and current row
        )::double / nullif(sum(daily_txn_count) over (
            partition by customer_token order by feature_date rows between 29 preceding and current row
        ), 0) as digital_ratio_30d,
        {{ rolling_window('daily_cross_border_count', 'customer_token', 'feature_date', 90) }} as cross_border_count_90d,
        age,
        tenure_months,
        feature_date - max(feature_date) over (
            partition by customer_token order by feature_date rows between unbounded preceding and current row
        ) as recency_days
    from daily_customer_transactions
)
select
    r.*,
    (
        select count(distinct s.merchant_category_group)
        from {{ ref('silver_transactions') }} s
        where s.customer_token = r.customer_token
          and s.transaction_date between r.feature_date - interval 29 day and r.feature_date
    ) as merchant_diversity_30d
from rolling_features r


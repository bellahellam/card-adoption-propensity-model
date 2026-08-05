{{
  config(
    materialized='external',
    format='parquet',
    location=s3_partitioned_location('silver', 'silver_transactions')
  )
}}

select
    customer_token,
    transaction_id,
    amount,
    currency,
    {{ currency_convert('amount', 'currency') }} as amount_usd,
    transaction_timestamp,
    cast(transaction_timestamp as date) as transaction_date,
    case
        when lower(channel) in ('mobile', 'app', 'online') then 'digital'
        when lower(channel) = 'branch' then 'branch'
        when lower(channel) = 'atm' then 'atm'
        when lower(channel) = 'agent' then 'agent'
        else 'unknown'
    end as channel_clean,
    case
        when lower(transaction_type) in ('purchase', 'pos') then 'purchase'
        when lower(transaction_type) in ('transfer', 'p2p') then 'transfer'
        when lower(transaction_type) = 'withdrawal' then 'withdrawal'
        when lower(transaction_type) = 'deposit' then 'deposit'
        when lower(transaction_type) = 'bill_pay' then 'bill_pay'
        else 'other'
    end as transaction_type_clean,
    merchant_mcc,
    case
        when merchant_mcc between 5411 and 5499 then 'grocery_retail'
        when merchant_mcc between 5812 and 5814 then 'food_beverage'
        when merchant_mcc between 4111 and 4789 then 'transport_travel'
        when merchant_mcc between 6010 and 6051 then 'financial_services'
        else 'other'
    end as merchant_category_group,
    is_cross_border,
    age,
    tenure_months,
    _ingestion_date,
    _ingestion_timestamp,
    _batch_id
from {{ ref('bronze_transactions') }}
where amount > 0
  and amount < 10000000


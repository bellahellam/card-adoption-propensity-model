{{
  config(
    materialized='external',
    format='parquet',
    location=s3_partitioned_location('bronze', 'bronze_transactions')
  )
}}

-- The raw transaction source is already tokenized; this model only fixes its
-- schema at the bronze contract boundary.
select
    cast(customer_token as varchar) as customer_token,
    cast(transaction_id as varchar) as transaction_id,
    cast(amount as double) as amount,
    cast(currency as varchar) as currency,
    cast(transaction_timestamp as timestamp) as transaction_timestamp,
    cast(channel as varchar) as channel,
    cast(transaction_type as varchar) as transaction_type,
    cast(merchant_mcc as integer) as merchant_mcc,
    cast(is_cross_border as boolean) as is_cross_border,
    cast(customer_age as integer) as age,
    cast(tenure_months as integer) as tenure_months,
    cast(_ingestion_date as date) as _ingestion_date,
    cast(_ingestion_timestamp as timestamp) as _ingestion_timestamp,
    cast(_batch_id as varchar) as _batch_id
from {{ source('raw', 'transactions') }}
where _ingestion_date = cast('{{ var("run_date", run_started_at.strftime("%Y-%m-%d")) }}' as date)

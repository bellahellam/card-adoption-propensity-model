{{
  config(
    materialized='external',
    format='parquet',
    location=s3_partitioned_location('marts', 'mart_campaign_segments')
  )
}}

-- This post-scoring mart is intentionally built after batch_score.py writes its
-- campaign scores. The transform job excludes it; the score job rebuilds its
-- dependency graph with `dbt build --select +mart_campaign_segments`.
with features as (
    select customer_token, feature_date
    from {{ ref('features_transactional') }}
),
scores as (
    select
        customer_token,
        propensity_score,
        score_decile,
        campaign_segment,
        cast(feature_date as date) as feature_date
    from {{ source('scoring', 'campaign_segments') }}
)
select
    f.customer_token,
    s.propensity_score,
    s.score_decile,
    s.campaign_segment,
    f.feature_date
from features f
inner join scores s
    on f.customer_token = s.customer_token
   and f.feature_date = s.feature_date

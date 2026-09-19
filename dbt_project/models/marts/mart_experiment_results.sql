{{
  config(
    materialized='external',
    format='parquet',
    location=s3_partitioned_location('marts', 'mart_experiment_results')
  )
}}

-- Customer-grain experiment fact table joining each holdout assignment to its
-- scored segment and observed adoption outcome. The Python lift-measurement job
-- (src/experiment/measure_lift.py) computes the statistical scorecard; this mart
-- gives analysts an auditable, queryable base for ad-hoc lift analysis in SQL.
with assignments as (
    select
        customer_token,
        campaign_id,
        experiment_group
    from {{ source('experiments', 'assignments') }}
    where experiment_group in ('treatment', 'control')
),
labels as (
    select
        customer_token,
        cast(adopted_card as integer) as adopted_card
    from {{ source('raw', 'customer_labels') }}
),
scores as (
    select
        customer_token,
        propensity_score,
        score_decile,
        campaign_segment
    from {{ source('scoring', 'campaign_segments') }}
)
select
    a.customer_token,
    a.campaign_id,
    a.experiment_group,
    coalesce(s.campaign_segment, 'UNKNOWN') as campaign_segment,
    s.propensity_score,
    s.score_decile,
    coalesce(l.adopted_card, 0) as adopted_card
from assignments a
left join labels l
    on a.customer_token = l.customer_token
left join scores s
    on a.customer_token = s.customer_token

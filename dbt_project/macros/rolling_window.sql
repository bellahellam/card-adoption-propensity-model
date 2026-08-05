{% macro rolling_window(column, partition_by, order_by, window_size) %}
    sum({{ column }}) over (
        partition by {{ partition_by }}
        order by {{ order_by }}
        rows between {{ window_size - 1 }} preceding and current row
    )
{% endmacro %}


{% macro s3_partitioned_location(layer, model_name) %}
    {% set run_date = var('run_date', run_started_at.strftime('%Y-%m-%d')) %}
    {% set parts = run_date.split('-') %}
    {{ return(
        's3://' ~ env_var('S3_BUCKET') ~ '/dbt/' ~ layer ~ '/' ~ model_name ~
        '/year=' ~ parts[0] ~ '/month=' ~ parts[1] ~ '/day=' ~ parts[2] ~ '/data.parquet'
    ) }}
{% endmacro %}


{% macro weekly_score_location() %}
    {% set run_date = var('run_date', run_started_at.strftime('%Y-%m-%d')) %}
    {% set parts = run_date.split('-') %}
    {{ return(
        's3://' ~ env_var('S3_BUCKET') ~ '/scores/weekly/year=' ~ parts[0] ~
        '/month=' ~ parts[1] ~ '/day=' ~ parts[2] ~ '/campaign_segments.parquet'
    ) }}
{% endmacro %}


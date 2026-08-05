{% macro currency_convert(amount, currency) %}
    case
        when {{ currency }} = 'KES' then {{ amount }} / 130.0
        when {{ currency }} = 'USD' then {{ amount }}
        else null
    end
{% endmacro %}


{% macro tokenization_note() %}
    {{ return('Customer tokens are created with HMAC-SHA256 before data reaches S3.') }}
{% endmacro %}


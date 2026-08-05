"""AWS Lambda handler for real-time Visa card-adoption propensity scoring."""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Mapping

import boto3
import joblib
import pandas as pd
from botocore.exceptions import BotoCoreError, ClientError


LOGGER = logging.getLogger(__name__)
MODEL_CACHE_PATH = Path("/tmp/visa_card_adoption_model.pkl")
REQUIRED_FIELDS = {
    "recency_days",
    "frequency_30d",
    "monetary_30d",
    "digital_ratio_30d",
    "cross_border_count_90d",
    "age",
    "tenure_months",
}
MODEL_INPUT_MAP = {
    "recency_days": "recency_days",
    "frequency_30d": "txn_count_30d",
    "monetary_30d": "volume_30d",
    "digital_ratio_30d": "digital_ratio_30d",
    "cross_border_count_90d": "cross_border_count_90d",
    "age": "age",
    "tenure_months": "tenure_months",
}
_MODEL_BUNDLE: dict[str, Any] | None = None


def response(status_code: int, body: Mapping[str, Any]) -> dict[str, Any]:
    """Build an API Gateway HTTP API-compatible JSON response."""
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json", "access-control-allow-origin": "*"},
        "body": json.dumps(body),
    }


def parse_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """Decode a Lambda HTTP API request body and validate its required fields."""
    body = event.get("body", event)
    if isinstance(body, str):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise ValueError("Request body must be valid JSON.") from error
    elif isinstance(body, Mapping):
        payload = dict(body)
    else:
        raise ValueError("Request body must be a JSON object.")
    missing = sorted(REQUIRED_FIELDS.difference(payload))
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")
    invalid = [field for field in REQUIRED_FIELDS if isinstance(payload[field], bool) or not isinstance(payload[field], (int, float))]
    if invalid:
        raise ValueError(f"Fields must be numeric: {', '.join(sorted(invalid))}")
    if not 0.0 <= float(payload["digital_ratio_30d"]) <= 1.0:
        raise ValueError("digital_ratio_30d must be between 0 and 1.")
    return payload


def load_model() -> dict[str, Any]:
    """Load the S3 model on cold start and retain it in process memory."""
    global _MODEL_BUNDLE
    if _MODEL_BUNDLE is not None:
        return _MODEL_BUNDLE
    bucket = os.environ.get("MODEL_BUCKET")
    model_key = os.environ.get("MODEL_KEY", "models/visa_card_adoption/model.pkl")
    if not bucket:
        raise RuntimeError("MODEL_BUCKET is not configured.")
    try:
        if not MODEL_CACHE_PATH.exists():
            boto3.client("s3").download_file(bucket, model_key, str(MODEL_CACHE_PATH))
        loaded = joblib.load(MODEL_CACHE_PATH)
    except (BotoCoreError, ClientError, OSError, ValueError, TypeError) as error:
        raise RuntimeError(f"Unable to load propensity model: {error}") from error
    if not isinstance(loaded, dict) or "model" not in loaded or "feature_columns" not in loaded:
        raise RuntimeError("Model artifact does not contain the expected model bundle.")
    _MODEL_BUNDLE = loaded
    return _MODEL_BUNDLE


def model_frame(payload: Mapping[str, Any], feature_columns: list[str]) -> pd.DataFrame:
    """Map public API field names to the feature order used by the trained model."""
    mapped = {model_name: float(payload[api_name]) for api_name, model_name in MODEL_INPUT_MAP.items()}
    missing = sorted(set(feature_columns).difference(mapped))
    if missing:
        raise ValueError(f"Model requires unsupported API features: {', '.join(missing)}")
    return pd.DataFrame([{column: mapped[column] for column in feature_columns}])


def recommendation_for_decile(decile: int) -> str:
    """Map a score decile to its campaign treatment."""
    if decile >= 9:
        return "TARGET_PREMIUM"
    if decile >= 7:
        return "TARGET_STANDARD"
    if decile >= 4:
        return "NURTURE"
    return "EXCLUDE"


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    """Return a propensity score, decile, campaign recommendation, and expected lift."""
    del context
    try:
        payload = parse_payload(event)
    except ValueError as error:
        return response(400, {"error": str(error)})
    try:
        bundle = load_model()
        feature_columns = bundle["feature_columns"]
        if not isinstance(feature_columns, list) or not all(isinstance(item, str) for item in feature_columns):
            raise RuntimeError("Model feature contract is invalid.")
        probability = float(bundle["model"].predict_proba(model_frame(payload, feature_columns))[:, 1][0])
    except (RuntimeError, ValueError, AttributeError, KeyError, TypeError) as error:
        LOGGER.exception("Unable to score propensity request: %s", error)
        return response(500, {"error": "Model load or scoring failed."})
    decile = min(10, max(1, int(math.ceil(probability * 10))))
    baseline_rate = float(os.environ.get("BASE_ADOPTION_RATE", "0.03"))
    expected_lift = probability / baseline_rate if baseline_rate > 0 else 0.0
    return response(
        200,
        {
            "propensity_score": round(probability, 4),
            "score_decile": decile,
            "recommendation": recommendation_for_decile(decile),
            "expected_lift": f"{expected_lift:.1f}x",
        },
    )


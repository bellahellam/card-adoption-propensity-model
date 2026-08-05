"""Score the latest dbt feature snapshot and publish campaign-ready S3 segments."""

from __future__ import annotations

import argparse
import io
import logging
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

import boto3
import joblib
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError


LOGGER = logging.getLogger(__name__)


def parse_date(value: str) -> date:
    """Parse an ISO date used to create deterministic score partitions."""
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Expected YYYY-MM-DD, received {value!r}") from error


def resolve_bucket(cli_bucket: str | None) -> str:
    """Resolve the configured S3 bucket without a hardcoded environment value."""
    bucket = cli_bucket or os.environ.get("S3_BUCKET")
    if not bucket:
        raise ValueError("S3_BUCKET must be set or passed as --bucket.")
    return bucket


def read_parquet_prefix(client: BaseClient, bucket: str, prefix: str) -> pd.DataFrame:
    """Load every Parquet object below a data-lake prefix."""
    frames: list[pd.DataFrame] = []
    paginator = client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for entry in page.get("Contents", []):
                key = str(entry["Key"])
                if key.endswith(".parquet"):
                    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
                    frames.append(pq.read_table(io.BytesIO(body)).to_pandas())
    except (BotoCoreError, ClientError, OSError) as error:
        raise RuntimeError(f"Unable to read s3://{bucket}/{prefix}: {error}") from error
    if not frames:
        raise FileNotFoundError(f"No Parquet objects found under s3://{bucket}/{prefix}")
    return pd.concat(frames, ignore_index=True)


def load_model(client: BaseClient, bucket: str, cache_dir: Path) -> dict[str, object]:
    """Download a model once to a reusable cache and deserialize its bundle."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    model_path = cache_dir / "model.pkl"
    try:
        if not model_path.exists():
            client.download_file(bucket, "models/visa_card_adoption/model.pkl", str(model_path))
    except (BotoCoreError, ClientError, OSError) as error:
        raise RuntimeError("Unable to download the current propensity model from S3.") from error
    bundle = joblib.load(model_path)
    if not isinstance(bundle, dict) or "model" not in bundle or "feature_columns" not in bundle:
        raise ValueError("model.pkl is not a supported Visa propensity model bundle.")
    return bundle


def assign_deciles(probabilities: pd.Series) -> pd.Series:
    """Assign integer deciles where 10 is highest propensity, even when ties occur."""
    if probabilities.empty:
        return pd.Series(dtype="int64")
    ranked = probabilities.rank(method="first")
    if len(probabilities) >= 10:
        return pd.qcut(ranked, q=10, labels=range(1, 11)).astype(int)
    return np.ceil(ranked / len(probabilities) * 10).astype(int)


def campaign_segment(deciles: pd.Series) -> pd.Series:
    """Map score deciles to the agreed marketing-treatment segment."""
    return pd.Series(
        np.select(
            [deciles >= 9, deciles >= 7, deciles >= 4],
            ["TARGET_PREMIUM", "TARGET_STANDARD", "NURTURE"],
            default="EXCLUDE",
        ),
        index=deciles.index,
    )


def score_features(features: pd.DataFrame, bundle: dict[str, object]) -> pd.DataFrame:
    """Predict probabilities and produce the campaign segment output contract."""
    model = bundle["model"]
    feature_columns = bundle["feature_columns"]
    if not isinstance(feature_columns, list) or not all(isinstance(column, str) for column in feature_columns):
        raise ValueError("Model feature_columns must be a list of strings.")
    missing_columns = set(feature_columns).difference(features.columns)
    if missing_columns:
        raise ValueError(f"Scoring features are missing columns: {sorted(missing_columns)}")
    if "customer_token" not in features.columns or "feature_date" not in features.columns:
        raise ValueError("Scoring features require customer_token and feature_date.")
    probability_values = model.predict_proba(features[feature_columns])[:, 1]  # type: ignore[union-attr]
    scored = features[["customer_token", "feature_date"]].copy()
    scored["propensity_score"] = probability_values
    scored["score_decile"] = assign_deciles(scored["propensity_score"])
    scored["campaign_segment"] = campaign_segment(scored["score_decile"])
    scored["feature_date"] = pd.to_datetime(scored["feature_date"]).dt.date
    return scored[["customer_token", "propensity_score", "score_decile", "campaign_segment", "feature_date"]]


def write_scores(client: BaseClient, bucket: str, scored: pd.DataFrame, score_date: date) -> str:
    """Write idempotent Hive-partitioned campaign scores with SSE-S3."""
    key = (
        f"scores/weekly/year={score_date:%Y}/month={score_date:%m}/"
        f"day={score_date:%d}/campaign_segments.parquet"
    )
    temporary_file = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
    temporary_path = Path(temporary_file.name)
    temporary_file.close()
    try:
        pq.write_table(pa.Table.from_pandas(scored, preserve_index=False), temporary_path, compression="zstd")
        client.upload_file(
            str(temporary_path),
            bucket,
            key,
            ExtraArgs={"ServerSideEncryption": "AES256", "ContentType": "application/octet-stream"},
        )
    except (BotoCoreError, ClientError, OSError) as error:
        raise RuntimeError(f"Unable to write s3://{bucket}/{key}: {error}") from error
    finally:
        temporary_path.unlink(missing_ok=True)
    return key


def build_parser() -> argparse.ArgumentParser:
    """Build command-line arguments for the weekly scoring process."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", help="S3 bucket; defaults to S3_BUCKET.")
    parser.add_argument("--feature-prefix", default="dbt/features/features_transactional/")
    parser.add_argument("--score-date", type=parse_date, default=datetime.now(timezone.utc).date())
    parser.add_argument("--model-cache-dir", default=os.environ.get("MODEL_CACHE_DIR", ".model-cache"))
    return parser


def main(arguments: Sequence[str] | None = None) -> None:
    """Load latest features and model, then write campaign-ready score segments."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(arguments)
    bucket = resolve_bucket(args.bucket)
    client = boto3.client("s3")
    features = read_parquet_prefix(client, bucket, args.feature_prefix)
    features["feature_date"] = pd.to_datetime(features["feature_date"], errors="coerce")
    latest_feature_date = features["feature_date"].max()
    if pd.isna(latest_feature_date):
        raise ValueError("No valid feature_date is available for batch scoring.")
    features = features.loc[features["feature_date"] == latest_feature_date].drop_duplicates("customer_token", keep="last")
    bundle = load_model(client, bucket, Path(args.model_cache_dir))
    scored = score_features(features, bundle)
    key = write_scores(client, bucket, scored, args.score_date)
    LOGGER.info("Scored %s customers; average propensity %.6f", len(scored), scored["propensity_score"].mean())
    LOGGER.info("Segment distribution: %s", scored["campaign_segment"].value_counts().to_dict())
    LOGGER.info("Wrote scores to s3://%s/%s", bucket, key)


if __name__ == "__main__":
    main()

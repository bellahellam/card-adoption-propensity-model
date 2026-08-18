"""Train and publish a calibrated Visa card-adoption propensity model from S3 data."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Sequence

import boto3
import joblib
import mlflow
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError
from sklearn.calibration import CalibratedClassifierCV
from xgboost import XGBClassifier

from evaluate import calculate_metrics, write_model_card


LOGGER = logging.getLogger(__name__)
FEATURE_COLUMNS = [
    "recency_days",
    "txn_count_30d",
    "volume_30d",
    "digital_ratio_30d",
    "cross_border_count_90d",
    "age",
    "tenure_months",
]


def resolve_bucket(cli_bucket: str | None) -> str:
    """Resolve the POC data lake bucket without storing credentials in code."""
    bucket = cli_bucket or os.environ.get("S3_BUCKET")
    if not bucket:
        raise ValueError("S3_BUCKET must be set or passed as --bucket.")
    return bucket


def read_parquet_prefix(client: BaseClient, bucket: str, prefix: str) -> pd.DataFrame:
    """Read all Parquet objects under an S3 prefix into one dataframe."""
    frames: list[pd.DataFrame] = []
    paginator = client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for entry in page.get("Contents", []):
                key = str(entry["Key"])
                if not key.endswith(".parquet"):
                    continue
                response = client.get_object(Bucket=bucket, Key=key)
                frames.append(pq.read_table(io.BytesIO(response["Body"].read())).to_pandas())
    except (BotoCoreError, ClientError, OSError) as error:
        raise RuntimeError(f"Unable to read s3://{bucket}/{prefix}: {error}") from error
    if not frames:
        raise FileNotFoundError(f"No Parquet objects found under s3://{bucket}/{prefix}")
    return pd.concat(frames, ignore_index=True)


def prepare_training_frame(features: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    """Join feature history to labels and validate the model input contract."""
    required_feature_columns = {"customer_token", "feature_date", *FEATURE_COLUMNS}
    missing_features = required_feature_columns.difference(features.columns)
    if missing_features:
        raise ValueError(f"Feature data is missing required columns: {sorted(missing_features)}")
    if not {"customer_token", "adopted_card"}.issubset(labels.columns):
        raise ValueError("Label data must contain customer_token and adopted_card.")

    if "_ingestion_date" in labels.columns:
        labels = labels.sort_values("_ingestion_date")
    label_frame = labels[["customer_token", "adopted_card"]].drop_duplicates("customer_token", keep="last")
    features = features.drop_duplicates(["customer_token", "feature_date"], keep="last")
    frame = features.merge(label_frame, on="customer_token", how="inner", validate="many_to_one")
    frame["feature_date"] = pd.to_datetime(frame["feature_date"], errors="coerce")
    frame = frame.dropna(subset=["feature_date", "adopted_card", *FEATURE_COLUMNS]).copy()
    frame["adopted_card"] = frame["adopted_card"].astype(int)
    if frame["adopted_card"].nunique() != 2:
        raise ValueError("Training labels must contain both adoption classes.")
    return frame


def temporal_split(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split the first 60 days for training and latest 30 days for testing."""
    latest_date = frame["feature_date"].max().normalize()
    cutoff_date = latest_date - pd.Timedelta(days=30)
    train_frame = frame.loc[frame["feature_date"] <= cutoff_date].copy()
    test_frame = frame.loc[frame["feature_date"] > cutoff_date].copy()
    if train_frame.empty or test_frame.empty:
        raise ValueError("Temporal split requires data on both sides of the 30-day cutoff.")
    if train_frame["adopted_card"].nunique() != 2 or test_frame["adopted_card"].nunique() != 2:
        raise ValueError("Both temporal partitions must contain positive and negative labels.")
    return train_frame, test_frame


def train_calibrated_model(train_frame: pd.DataFrame) -> CalibratedClassifierCV:
    """Fit the requested weighted XGBoost classifier and isotonic calibrator."""
    labels = train_frame["adopted_card"].to_numpy()
    positive_count = int(labels.sum())
    negative_count = len(labels) - positive_count
    if positive_count == 0:
        raise ValueError("Cannot train a propensity model without positive labels.")
    classifier = XGBClassifier(
        max_depth=4,
        learning_rate=0.05,
        n_estimators=200,
        scale_pos_weight=negative_count / positive_count,
        eval_metric="aucpr",
        objective="binary:logistic",
        random_state=42,
        n_jobs=4,
        tree_method="hist",
    )
    model = CalibratedClassifierCV(estimator=classifier, method="isotonic", cv=3)
    model.fit(train_frame[FEATURE_COLUMNS], labels)
    return model


def upload_file(client: BaseClient, bucket: str, key: str, path: Path, content_type: str) -> None:
    """Upload a local model artifact to the POC bucket using SSE-S3."""
    try:
        client.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={"ServerSideEncryption": "AES256", "ContentType": content_type},
        )
    except (BotoCoreError, ClientError, OSError) as error:
        raise RuntimeError(f"Unable to upload s3://{bucket}/{key}: {error}") from error


def build_parser() -> argparse.ArgumentParser:
    """Create command-line arguments for training in GitHub Actions or locally."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", help="S3 bucket; defaults to S3_BUCKET.")
    parser.add_argument(
        "--feature-prefix",
        default="dbt/gold/gold_customer_features/",
        help="Historical feature prefix. Gold is required for the 60/30 temporal split.",
    )
    parser.add_argument("--label-prefix", default="raw/customer_labels/")
    parser.add_argument(
        "--mlflow-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db"),
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> None:
    """Load S3 data, train and evaluate the model, then publish its artifacts."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(arguments)
    bucket = resolve_bucket(args.bucket)
    client = boto3.client("s3")
    # The latest features_transactional dataset is intentionally used only for scoring;
    # its one-day filter cannot support the required 60/30 temporal validation.
    features = read_parquet_prefix(client, bucket, args.feature_prefix)
    labels = read_parquet_prefix(client, bucket, args.label_prefix)
    frame = prepare_training_frame(features, labels)
    train_frame, test_frame = temporal_split(frame)
    model = train_calibrated_model(train_frame)
    probabilities = model.predict_proba(test_frame[FEATURE_COLUMNS])[:, 1]
    metrics = calculate_metrics(test_frame["adopted_card"].to_numpy(), probabilities)

    mlflow.set_tracking_uri(args.mlflow_uri)
    mlflow.set_experiment("visa_card_adoption")
    with tempfile.TemporaryDirectory(prefix="visa-propensity-") as temporary_directory:
        artifact_dir = Path(temporary_directory)
        model_path = artifact_dir / "model.pkl"
        card_path = artifact_dir / "model_card.json"
        joblib.dump({"model": model, "feature_columns": FEATURE_COLUMNS}, model_path)
        write_model_card(card_path, FEATURE_COLUMNS, metrics)
        model_cache_dir = Path(os.environ.get("MODEL_CACHE_DIR", ".model-cache"))
        model_cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(model_path, model_cache_dir / "model.pkl")
        with mlflow.start_run():
            mlflow.log_params(
                {
                    "max_depth": 4,
                    "learning_rate": 0.05,
                    "n_estimators": 200,
                    "calibration": "isotonic",
                    "train_rows": len(train_frame),
                    "test_rows": len(test_frame),
                }
            )
            mlflow.log_metrics(metrics)
            mlflow.log_artifact(str(model_path), artifact_path="model")
            mlflow.log_artifact(str(card_path), artifact_path="model")
        upload_file(client, bucket, "models/visa_card_adoption/model.pkl", model_path, "application/octet-stream")
        upload_file(client, bucket, "models/visa_card_adoption/model_card.json", card_path, "application/json")

    LOGGER.info("Evaluation metrics: %s", json.dumps(metrics, sort_keys=True))


if __name__ == "__main__":
    main()

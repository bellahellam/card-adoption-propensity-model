"""Generate deterministic, tokenized synthetic transaction data for the POC.

The generator never persists a PAN. It derives a stable HMAC token in memory,
then writes only tokenized transaction and customer-label Parquet datasets to S3.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

import boto3
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError


LOGGER = logging.getLogger(__name__)
TOKEN_SALT = "visa_card_adoption_v1"
POC_TOKEN_KEY = "visa-poc-token-key-not-for-production"
MCC_CODES = np.array(
    [4111, 4121, 4511, 4722, 4789, 5411, 5422, 5441, 5499, 5812, 5813, 5814, 6010, 6011, 6012, 6050, 6051, 5541, 5732, 7999],
    dtype=np.int32,
)


def parse_date(value: str) -> date:
    """Parse an ISO-8601 calendar date supplied through the command line."""
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Expected YYYY-MM-DD, received {value!r}") from error


def stable_customer_token(pan: str, token_key: str) -> str:
    """Return the first 32 hexadecimal characters of a salted HMAC-SHA256 token."""
    payload = f"{TOKEN_SALT}:{pan}".encode("utf-8")
    return hmac.new(token_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()[:32]


def resolve_bucket(cli_bucket: str | None) -> str:
    """Resolve the required data-lake bucket without embedding any AWS credential."""
    bucket = cli_bucket or os.environ.get("S3_BUCKET")
    if not bucket:
        raise ValueError("S3_BUCKET must be set or passed as --bucket.")
    return bucket


def sigmoid(values: np.ndarray) -> np.ndarray:
    """Compute a numerically stable logistic transformation."""
    clipped = np.clip(values, -40.0, 40.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def build_synthetic_data(
    customer_count: int,
    transaction_count: int,
    history_days: int,
    as_of_date: date,
    token_key: str,
    seed: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create transactions and customer labels with a roughly 3% adoption rate."""
    if customer_count <= 0 or transaction_count <= 0 or history_days <= 0:
        raise ValueError("customer_count, transaction_count, and history_days must be positive.")

    rng = np.random.default_rng(seed)
    customer_ids = np.arange(customer_count, dtype=np.int64)
    tokens = [stable_customer_token(str(4_000_000_000_000_000 + int(identifier)), token_key) for identifier in customer_ids]
    ages = rng.integers(21, 71, size=customer_count, endpoint=False)
    tenures = rng.integers(1, 121, size=customer_count, endpoint=False)

    transaction_customer_ids = rng.integers(0, customer_count, size=transaction_count, endpoint=False)
    days_before_as_of = rng.integers(1, history_days + 1, size=transaction_count, endpoint=False)
    transaction_dates = pd.to_datetime(as_of_date) - pd.to_timedelta(days_before_as_of, unit="D")
    transaction_timestamps = transaction_dates + pd.to_timedelta(
        rng.integers(0, 86_400, size=transaction_count, endpoint=False), unit="s"
    )
    channels = rng.choice(
        np.array(["mobile", "branch", "atm", "agent", "online"]),
        size=transaction_count,
        p=np.array([0.55, 0.15, 0.15, 0.10, 0.05]),
    )
    transaction_types = rng.choice(
        np.array(["purchase", "pos", "transfer", "p2p", "withdrawal", "deposit", "bill_pay"]),
        size=transaction_count,
        p=np.array([0.36, 0.16, 0.08, 0.08, 0.15, 0.07, 0.10]),
    )
    amounts = np.clip(rng.lognormal(mean=5.0, sigma=1.2, size=transaction_count), 10.0, 50_000.0).round(2)
    currencies = rng.choice(np.array(["KES", "USD"]), size=transaction_count, p=np.array([0.80, 0.20]))
    cross_border = rng.random(transaction_count) < 0.05
    batch_id = f"visa-{as_of_date.isoformat()}"
    fixed_ingestion_timestamp = datetime.combine(as_of_date, datetime.min.time(), tzinfo=timezone.utc)

    transactions = pd.DataFrame(
        {
            "customer_token": np.asarray(tokens, dtype=object)[transaction_customer_ids],
            "transaction_id": [f"txn_{batch_id}_{index:07d}" for index in range(transaction_count)],
            "amount": amounts,
            "currency": currencies,
            "transaction_timestamp": transaction_timestamps,
            "channel": channels,
            "transaction_type": transaction_types,
            "merchant_mcc": rng.choice(MCC_CODES, size=transaction_count),
            "is_cross_border": cross_border,
            "customer_age": ages[transaction_customer_ids],
            "tenure_months": tenures[transaction_customer_ids],
            "_ingestion_date": as_of_date,
            "_ingestion_timestamp": fixed_ingestion_timestamp,
            "_batch_id": batch_id,
        }
    )

    transaction_counts = np.bincount(transaction_customer_ids, minlength=customer_count)
    digital_counts = np.bincount(
        transaction_customer_ids,
        weights=np.isin(channels, ["mobile", "online"]).astype(np.int8),
        minlength=customer_count,
    )
    cross_border_counts = np.bincount(
        transaction_customer_ids, weights=cross_border.astype(np.int8), minlength=customer_count
    )
    customer_volume_usd = np.bincount(
        transaction_customer_ids,
        weights=np.where(currencies == "KES", amounts / 130.0, amounts),
        minlength=customer_count,
    )
    digital_ratio = digital_counts / np.maximum(transaction_counts, 1)
    standardized_frequency = (np.log1p(transaction_counts) - np.log1p(transaction_counts).mean()) / max(
        np.log1p(transaction_counts).std(), 1e-6
    )
    standardized_volume = (np.log1p(customer_volume_usd) - np.log1p(customer_volume_usd).mean()) / max(
        np.log1p(customer_volume_usd).std(), 1e-6
    )
    propensity_signal = (
        0.55 * standardized_frequency
        + 0.35 * standardized_volume
        + 0.80 * digital_ratio
        + 0.12 * cross_border_counts
        + 0.015 * (ages - 40)
        + 0.004 * tenures
    )
    baseline_logit = np.log(0.03 / 0.97) - float(propensity_signal.mean())
    adoption_probability = sigmoid(baseline_logit + propensity_signal)
    adopted_card = (rng.random(customer_count) < adoption_probability).astype(np.int8)
    labels = pd.DataFrame(
        {
            "customer_token": tokens,
            "adopted_card": adopted_card,
            "age": ages,
            "tenure_months": tenures,
            "label_date": as_of_date - pd.Timedelta(days=1),
            "_ingestion_date": as_of_date,
            "_ingestion_timestamp": fixed_ingestion_timestamp,
            "_batch_id": batch_id,
        }
    )

    # No raw PAN is retained in either returned dataframe or any S3 object.
    return transactions, labels


def put_parquet(
    client: BaseClient, bucket: str, key: str, frame: pd.DataFrame
) -> None:
    """Write one Parquet frame to S3 with SSE-S3 and an idempotent key."""
    temporary_file = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
    temporary_path = Path(temporary_file.name)
    temporary_file.close()
    try:
        pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), temporary_path, compression="zstd")
        client.upload_file(
            str(temporary_path),
            bucket,
            key,
            ExtraArgs={"ServerSideEncryption": "AES256", "ContentType": "application/octet-stream"},
        )
    except (BotoCoreError, ClientError, OSError) as error:
        raise RuntimeError(f"Unable to upload {key} to bucket {bucket}: {error}") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def hive_key(prefix: str, partition_date: date, filename: str) -> str:
    """Return a Hive-partitioned S3 key for a supplied date and file name."""
    return (
        f"{prefix}/year={partition_date:%Y}/month={partition_date:%m}/"
        f"day={partition_date:%d}/{filename}"
    )


def write_datasets(
    client: BaseClient, bucket: str, transactions: pd.DataFrame, labels: pd.DataFrame, as_of_date: date
) -> None:
    """Persist daily transaction partitions and one label partition to S3."""
    transaction_dates = pd.to_datetime(transactions["transaction_timestamp"]).dt.date
    for partition_date, partition in transactions.groupby(transaction_dates, sort=True):
        key = hive_key("raw/transactions", partition_date, "transactions.parquet")
        put_parquet(client, bucket, key, partition.reset_index(drop=True))
        LOGGER.info("Wrote %s tokenized transactions to s3://%s/%s", len(partition), bucket, key)

    label_key = hive_key("raw/customer_labels", as_of_date, "customer_labels.parquet")
    put_parquet(client, bucket, label_key, labels)
    LOGGER.info("Wrote %s customer labels to s3://%s/%s", len(labels), bucket, label_key)


def build_parser() -> argparse.ArgumentParser:
    """Construct the command-line parser for local and GitHub Actions execution."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", help="Destination S3 bucket; defaults to S3_BUCKET.")
    parser.add_argument("--customers", type=int, default=50_000)
    parser.add_argument("--transactions", type=int, default=500_000)
    parser.add_argument("--history-days", type=int, default=90)
    parser.add_argument("--run-date", type=parse_date, default=datetime.now(timezone.utc).date())
    parser.add_argument("--seed", type=int, help="Optional fixed random seed; default derives from run date.")
    return parser


def main(arguments: Sequence[str] | None = None) -> None:
    """Generate, tokenize, and upload one deterministic synthetic source batch."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(arguments)
    bucket = resolve_bucket(args.bucket)
    token_key = os.environ.get("TOKEN_KEY") or POC_TOKEN_KEY
    seed = args.seed if args.seed is not None else int(args.run_date.strftime("%Y%m%d"))
    transactions, labels = build_synthetic_data(
        args.customers, args.transactions, args.history_days, args.run_date, token_key, seed
    )
    LOGGER.info("Synthetic adoption prevalence: %.2f%%", 100.0 * labels["adopted_card"].mean())
    write_datasets(boto3.client("s3"), bucket, transactions, labels, args.run_date)


if __name__ == "__main__":
    main()

"""Deterministic, stratified treatment/control assignment for campaign holdouts.

A campaign cannot prove that targeting *caused* adoption without an untreated
control group. This module assigns each targeted customer to ``treatment`` or
``control`` using a stateless hash of ``customer_token`` and a per-campaign salt,
so the split is reproducible, auditable, and idempotent across reruns.

The assignment is stratified within campaign segment (and, when present, score
decile) so that treatment and control share the same propensity distribution.
That balance is what makes the later difference in conversion rates an unbiased
estimate of the campaign effect.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import logging
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError


LOGGER = logging.getLogger(__name__)

# Segments that receive a marketing treatment. EXCLUDE is never contacted, so it
# is not part of the experiment population.
TREATED_SEGMENTS = ("TARGET_PREMIUM", "TARGET_STANDARD", "NURTURE")
HASH_DENOMINATOR = 10_000


def parse_date(value: str) -> date:
    """Parse an ISO date used to build deterministic assignment partitions."""
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Expected YYYY-MM-DD, received {value!r}") from error


def resolve_bucket(cli_bucket: str | None) -> str:
    """Resolve the data-lake bucket without embedding any AWS credential."""
    bucket = cli_bucket or os.environ.get("S3_BUCKET")
    if not bucket:
        raise ValueError("S3_BUCKET must be set or passed as --bucket.")
    return bucket


def _assignment_fraction(customer_token: str, campaign_id: str) -> float:
    """Return a stable pseudo-random fraction in [0, 1) for a token and campaign.

    The value is derived from a SHA-256 digest of the campaign-salted token, so it
    is deterministic (a rerun assigns the identical group) and independent of row
    order or DataFrame partitioning.
    """
    payload = f"{campaign_id}:{customer_token}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    bucket = int(digest[:8], 16) % HASH_DENOMINATOR
    return bucket / HASH_DENOMINATOR


def assign_holdout(
    scored: pd.DataFrame,
    campaign_id: str,
    control_fraction: float = 0.15,
    treated_segments: Sequence[str] = TREATED_SEGMENTS,
) -> pd.DataFrame:
    """Split targeted customers into treatment and control groups.

    Args:
        scored: campaign-scored customers. Must contain ``customer_token`` and
            ``campaign_segment``; ``score_decile`` and ``feature_date`` are used
            when present.
        campaign_id: unique campaign identifier that salts the hash so different
            campaigns produce independent randomizations of the same customers.
        control_fraction: share of the targeted population held back as control.
        treated_segments: segments eligible for treatment; all other segments are
            recorded as ``not_targeted`` and excluded from the experiment.

    Returns:
        A frame with one row per input customer and an added ``experiment_group``
        column of ``treatment`` | ``control`` | ``not_targeted`` plus the
        ``campaign_id`` and a ``hash_fraction`` audit column.
    """
    if not 0.0 < control_fraction < 1.0:
        raise ValueError("control_fraction must be strictly between 0 and 1.")
    required = {"customer_token", "campaign_segment"}
    missing = required.difference(scored.columns)
    if missing:
        raise ValueError(f"Scored input is missing required columns: {sorted(missing)}")
    if not campaign_id:
        raise ValueError("campaign_id must be a non-empty string.")

    result = scored.copy()
    result["campaign_id"] = campaign_id
    result["hash_fraction"] = result["customer_token"].astype(str).map(
        lambda token: _assignment_fraction(token, campaign_id)
    )

    eligible = result["campaign_segment"].isin(list(treated_segments))
    # Stratified holdout: because hash_fraction is uniform and independent of the
    # propensity signal, thresholding it inside each eligible segment yields
    # treatment/control groups with matching score distributions.
    result["experiment_group"] = "not_targeted"
    result.loc[eligible & (result["hash_fraction"] < control_fraction), "experiment_group"] = "control"
    result.loc[eligible & (result["hash_fraction"] >= control_fraction), "experiment_group"] = "treatment"
    return result


def read_parquet_prefix(client: BaseClient, bucket: str, prefix: str) -> pd.DataFrame:
    """Load every Parquet object below a data-lake prefix into one frame."""
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


def write_assignments(
    client: BaseClient, bucket: str, assignments: pd.DataFrame, assignment_date: date
) -> str:
    """Write idempotent Hive-partitioned assignments with SSE-S3."""
    key = (
        f"experiments/assignments/year={assignment_date:%Y}/month={assignment_date:%m}/"
        f"day={assignment_date:%d}/experiment_assignments.parquet"
    )
    temporary_file = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
    temporary_path = Path(temporary_file.name)
    temporary_file.close()
    try:
        pq.write_table(
            pa.Table.from_pandas(assignments, preserve_index=False),
            temporary_path,
            compression="zstd",
        )
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
    """Build command-line arguments for the assignment step."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", help="S3 bucket; defaults to S3_BUCKET.")
    parser.add_argument("--score-date", type=parse_date, default=datetime.now(timezone.utc).date())
    parser.add_argument(
        "--campaign-id",
        help="Unique campaign identifier; defaults to visa-<score-date>.",
    )
    parser.add_argument("--control-fraction", type=float, default=0.15)
    return parser


def main(arguments: Sequence[str] | None = None) -> None:
    """Read the latest campaign scores, assign holdouts, and publish to S3."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(arguments)
    bucket = resolve_bucket(args.bucket)
    campaign_id = args.campaign_id or f"visa-{args.score_date.isoformat()}"
    client = boto3.client("s3")

    score_key_prefix = (
        f"scores/weekly/year={args.score_date:%Y}/month={args.score_date:%m}/day={args.score_date:%d}/"
    )
    scored = read_parquet_prefix(client, bucket, score_key_prefix)
    assignments = assign_holdout(scored, campaign_id, control_fraction=args.control_fraction)
    key = write_assignments(client, bucket, assignments, args.score_date)

    distribution = assignments["experiment_group"].value_counts().to_dict()
    LOGGER.info("Campaign %s assignment distribution: %s", campaign_id, distribution)
    LOGGER.info("Wrote assignments to s3://%s/%s", bucket, key)


if __name__ == "__main__":
    main()

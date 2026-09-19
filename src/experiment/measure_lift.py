"""Estimate the causal effect of a campaign from treatment/control outcomes.

This module answers "did targeting work?" by comparing the observed adoption rate
of the treated group against the held-out control group. Because assignment was
randomized within segment (see ``assign.py``), the difference in rates is an
unbiased estimate of the Average Treatment Effect (ATE) - the *incremental*
adoption caused by the campaign rather than the adoption the propensity model
could merely rank.

Outputs, per overall campaign and per segment:
- control / treatment conversion rates and sample sizes
- absolute lift (ATE) and relative lift
- a two-proportion z-test statistic, p-value, and confidence interval
- the minimum detectable effect (MDE) for the realized sample and a power flag
- optional CUPED-adjusted lift that uses a pre-period covariate to shrink variance
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import boto3
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError
from scipy import stats


LOGGER = logging.getLogger(__name__)
DEFAULT_ALPHA = 0.05
DEFAULT_POWER = 0.80


def parse_date(value: str) -> date:
    """Parse an ISO date used to build deterministic result partitions."""
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


def two_proportion_z_test(
    control_successes: int,
    control_total: int,
    treatment_successes: int,
    treatment_total: int,
    alpha: float = DEFAULT_ALPHA,
) -> dict[str, float]:
    """Run a two-sided two-proportion z-test for treatment minus control lift.

    Returns the two conversion rates, the absolute and relative lift, the z
    statistic, its p-value, and a (1 - alpha) confidence interval on the absolute
    lift computed with the unpooled standard error.
    """
    if control_total <= 0 or treatment_total <= 0:
        raise ValueError("Both groups must contain at least one observation.")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be strictly between 0 and 1.")

    control_rate = control_successes / control_total
    treatment_rate = treatment_successes / treatment_total
    absolute_lift = treatment_rate - control_rate

    pooled_rate = (control_successes + treatment_successes) / (control_total + treatment_total)
    pooled_se = math.sqrt(
        pooled_rate * (1.0 - pooled_rate) * (1.0 / control_total + 1.0 / treatment_total)
    )
    if pooled_se == 0.0:
        z_statistic = 0.0
        p_value = 1.0
    else:
        z_statistic = absolute_lift / pooled_se
        p_value = 2.0 * (1.0 - stats.norm.cdf(abs(z_statistic)))

    unpooled_se = math.sqrt(
        control_rate * (1.0 - control_rate) / control_total
        + treatment_rate * (1.0 - treatment_rate) / treatment_total
    )
    z_critical = stats.norm.ppf(1.0 - alpha / 2.0)
    margin = z_critical * unpooled_se

    return {
        "control_rate": float(control_rate),
        "treatment_rate": float(treatment_rate),
        "absolute_lift": float(absolute_lift),
        "relative_lift": float(absolute_lift / control_rate) if control_rate > 0 else 0.0,
        "z_statistic": float(z_statistic),
        "p_value": float(p_value),
        "confidence_low": float(absolute_lift - margin),
        "confidence_high": float(absolute_lift + margin),
        "significant": bool(p_value < alpha),
    }


def minimum_detectable_effect(
    baseline_rate: float,
    control_total: int,
    treatment_total: int,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> float:
    """Return the smallest absolute lift detectable at the given alpha and power.

    A campaign that reports a non-significant result is only informative if it had
    the power to detect a meaningful effect. Comparing the observed lift to this
    MDE distinguishes "no effect" from "sample too small".
    """
    if control_total <= 0 or treatment_total <= 0:
        return float("inf")
    z_alpha = stats.norm.ppf(1.0 - alpha / 2.0)
    z_power = stats.norm.ppf(power)
    variance = baseline_rate * (1.0 - baseline_rate)
    effective_n = 1.0 / (1.0 / control_total + 1.0 / treatment_total)
    if effective_n <= 0.0:
        return float("inf")
    return float((z_alpha + z_power) * math.sqrt(variance / effective_n))


def cuped_adjusted_lift(frame: pd.DataFrame, covariate: str) -> dict[str, float] | None:
    """Compute a CUPED variance-reduced lift using a pre-period covariate.

    CUPED regresses the outcome on a pre-treatment covariate that is unaffected by
    the campaign, then removes that explained variance before comparing groups.
    The point estimate is unchanged in expectation but the variance - and thus the
    confidence interval - shrinks, so significance is reached with a smaller
    holdout. Returns ``None`` when the covariate has no usable variance.
    """
    if covariate not in frame.columns:
        return None
    covariate_values = pd.to_numeric(frame[covariate], errors="coerce")
    outcome = pd.to_numeric(frame["adopted_card"], errors="coerce")
    valid = covariate_values.notna() & outcome.notna()
    covariate_values = covariate_values[valid].to_numpy(dtype=float)
    outcome = outcome[valid].to_numpy(dtype=float)
    groups = frame.loc[valid, "experiment_group"].to_numpy()
    covariate_variance = float(np.var(covariate_values))
    if covariate_variance == 0.0 or len(outcome) < 2:
        return None

    theta = float(np.cov(outcome, covariate_values)[0, 1] / covariate_variance)
    covariate_mean = float(np.mean(covariate_values))
    adjusted = outcome - theta * (covariate_values - covariate_mean)

    treatment_mask = groups == "treatment"
    control_mask = groups == "control"
    if treatment_mask.sum() == 0 or control_mask.sum() == 0:
        return None
    adjusted_lift = float(adjusted[treatment_mask].mean() - adjusted[control_mask].mean())
    variance_reduction = float(1.0 - np.var(adjusted) / np.var(outcome)) if np.var(outcome) > 0 else 0.0
    return {
        "cuped_theta": theta,
        "cuped_adjusted_lift": adjusted_lift,
        "cuped_variance_reduction": variance_reduction,
    }


def _group_counts(frame: pd.DataFrame, group: str) -> tuple[int, int]:
    """Return (successes, total) adoptions for one experiment group."""
    subset = frame.loc[frame["experiment_group"] == group]
    total = int(len(subset))
    successes = int(subset["adopted_card"].sum()) if total else 0
    return successes, total


def evaluate_experiment(
    frame: pd.DataFrame,
    alpha: float = DEFAULT_ALPHA,
    cuped_covariate: str | None = "volume_30d",
) -> dict[str, Any]:
    """Evaluate campaign lift overall and per segment from joined outcomes.

    Args:
        frame: one row per assigned customer with ``experiment_group``,
            ``adopted_card`` (0/1 observed outcome), and ``campaign_segment``.
        alpha: significance level for the z-test and confidence interval.
        cuped_covariate: optional pre-period numeric column for CUPED adjustment.

    Returns:
        A nested result dict with an ``overall`` block and a ``segments`` list.
    """
    required = {"experiment_group", "adopted_card", "campaign_segment"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Outcome frame is missing required columns: {sorted(missing)}")

    experiment = frame.loc[frame["experiment_group"].isin(["treatment", "control"])].copy()
    experiment["adopted_card"] = pd.to_numeric(experiment["adopted_card"], errors="coerce").fillna(0).astype(int)
    if experiment.empty:
        raise ValueError("No treatment or control rows are available to evaluate.")

    def _evaluate(subset: pd.DataFrame) -> dict[str, Any]:
        control_successes, control_total = _group_counts(subset, "control")
        treatment_successes, treatment_total = _group_counts(subset, "treatment")
        if control_total == 0 or treatment_total == 0:
            return {
                "control_total": control_total,
                "treatment_total": treatment_total,
                "evaluable": False,
                "reason": "Both a treatment and a control group are required.",
            }
        test = two_proportion_z_test(
            control_successes, control_total, treatment_successes, treatment_total, alpha
        )
        baseline_rate = control_successes / control_total
        mde = minimum_detectable_effect(baseline_rate, control_total, treatment_total, alpha)
        result: dict[str, Any] = {
            "control_total": control_total,
            "treatment_total": treatment_total,
            "control_adoptions": control_successes,
            "treatment_adoptions": treatment_successes,
            "incremental_adoptions": float(test["absolute_lift"] * treatment_total),
            "minimum_detectable_effect": mde,
            "well_powered": bool(abs(test["absolute_lift"]) >= mde),
            "evaluable": True,
            **test,
        }
        if cuped_covariate:
            cuped = cuped_adjusted_lift(subset, cuped_covariate)
            if cuped is not None:
                result.update(cuped)
        return result

    overall = _evaluate(experiment)
    segments = []
    for segment_name, segment_frame in experiment.groupby("campaign_segment", sort=True):
        segment_result = _evaluate(segment_frame)
        segment_result["campaign_segment"] = str(segment_name)
        segments.append(segment_result)

    return {"alpha": alpha, "overall": overall, "segments": segments}


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


def build_outcome_frame(
    assignments: pd.DataFrame, labels: pd.DataFrame, features: pd.DataFrame | None
) -> pd.DataFrame:
    """Join assignments to observed outcomes and an optional CUPED covariate."""
    if "customer_token" not in assignments.columns:
        raise ValueError("Assignments must contain customer_token.")
    if not {"customer_token", "adopted_card"}.issubset(labels.columns):
        raise ValueError("Labels must contain customer_token and adopted_card.")

    label_frame = labels[["customer_token", "adopted_card"]].drop_duplicates("customer_token", keep="last")
    merged = assignments.merge(label_frame, on="customer_token", how="left", validate="many_to_one")
    merged["adopted_card"] = pd.to_numeric(merged["adopted_card"], errors="coerce").fillna(0).astype(int)

    if features is not None and "customer_token" in features.columns:
        covariate_columns = [c for c in ("volume_30d", "txn_count_30d") if c in features.columns]
        if covariate_columns:
            covariate_frame = features[["customer_token", *covariate_columns]].drop_duplicates(
                "customer_token", keep="last"
            )
            merged = merged.merge(covariate_frame, on="customer_token", how="left", validate="many_to_one")
    return merged


def write_scorecard(
    client: BaseClient, bucket: str, scorecard: dict[str, Any], result_date: date, campaign_id: str
) -> str:
    """Write the campaign scorecard JSON to S3 with SSE-S3."""
    key = (
        f"experiments/results/year={result_date:%Y}/month={result_date:%m}/"
        f"day={result_date:%d}/campaign_scorecard.json"
    )
    temporary_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    temporary_path = Path(temporary_file.name)
    temporary_file.close()
    try:
        temporary_path.write_text(json.dumps(scorecard, indent=2, sort_keys=True), encoding="utf-8")
        client.upload_file(
            str(temporary_path),
            bucket,
            key,
            ExtraArgs={"ServerSideEncryption": "AES256", "ContentType": "application/json"},
        )
    except (BotoCoreError, ClientError, OSError) as error:
        raise RuntimeError(f"Unable to write s3://{bucket}/{key}: {error}") from error
    finally:
        temporary_path.unlink(missing_ok=True)
    return key


def log_to_mlflow(scorecard: dict[str, Any], mlflow_uri: str, campaign_id: str) -> None:
    """Record the overall campaign lift metrics in the MLflow tracking store."""
    try:
        import mlflow
    except ImportError:  # pragma: no cover - mlflow is a declared dependency
        LOGGER.warning("mlflow is unavailable; skipping experiment tracking.")
        return
    overall = scorecard.get("overall", {})
    if not overall.get("evaluable", False):
        LOGGER.warning("Overall result is not evaluable; skipping MLflow logging.")
        return
    mlflow.set_tracking_uri(mlflow_uri)
    mlflow.set_experiment("visa_campaign_lift")
    with mlflow.start_run(run_name=campaign_id):
        mlflow.log_params({"campaign_id": campaign_id, "alpha": scorecard.get("alpha", DEFAULT_ALPHA)})
        mlflow.log_metrics(
            {
                "absolute_lift": overall["absolute_lift"],
                "relative_lift": overall["relative_lift"],
                "p_value": overall["p_value"],
                "control_rate": overall["control_rate"],
                "treatment_rate": overall["treatment_rate"],
                "incremental_adoptions": overall["incremental_adoptions"],
                "minimum_detectable_effect": overall["minimum_detectable_effect"],
            }
        )


def build_parser() -> argparse.ArgumentParser:
    """Build command-line arguments for the lift-measurement step."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", help="S3 bucket; defaults to S3_BUCKET.")
    parser.add_argument(
        "--assignment-date",
        type=parse_date,
        default=datetime.now(timezone.utc).date(),
        help="Date partition of the experiment assignments to evaluate.",
    )
    parser.add_argument(
        "--outcome-date",
        type=parse_date,
        default=None,
        help="Label partition to read outcomes from; defaults to the assignment date.",
    )
    parser.add_argument("--campaign-id", help="Campaign identifier; defaults to visa-<assignment-date>.")
    parser.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    parser.add_argument(
        "--mlflow-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db"),
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> None:
    """Load assignments and outcomes, estimate lift, and publish the scorecard."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(arguments)
    bucket = resolve_bucket(args.bucket)
    campaign_id = args.campaign_id or f"visa-{args.assignment_date.isoformat()}"
    outcome_date = args.outcome_date or args.assignment_date
    client = boto3.client("s3")

    assignment_prefix = (
        f"experiments/assignments/year={args.assignment_date:%Y}/month={args.assignment_date:%m}/"
        f"day={args.assignment_date:%d}/"
    )
    label_prefix = (
        f"raw/customer_labels/year={outcome_date:%Y}/month={outcome_date:%m}/day={outcome_date:%d}/"
    )
    assignments = read_parquet_prefix(client, bucket, assignment_prefix)
    labels = read_parquet_prefix(client, bucket, label_prefix)
    try:
        features = read_parquet_prefix(client, bucket, "dbt/features/features_transactional/")
    except FileNotFoundError:
        LOGGER.warning("No feature snapshot found for CUPED; proceeding without variance reduction.")
        features = None

    outcomes = build_outcome_frame(assignments, labels, features)
    scorecard = evaluate_experiment(outcomes, alpha=args.alpha)
    scorecard["campaign_id"] = campaign_id
    scorecard["assignment_date"] = args.assignment_date.isoformat()
    scorecard["outcome_date"] = outcome_date.isoformat()

    key = write_scorecard(client, bucket, scorecard, args.assignment_date, campaign_id)
    log_to_mlflow(scorecard, args.mlflow_uri, campaign_id)

    overall = scorecard["overall"]
    if overall.get("evaluable"):
        LOGGER.info(
            "Campaign %s absolute lift %.4f (p=%.4f, significant=%s, incremental adoptions=%.1f)",
            campaign_id,
            overall["absolute_lift"],
            overall["p_value"],
            overall["significant"],
            overall["incremental_adoptions"],
        )
    LOGGER.info("Wrote campaign scorecard to s3://%s/%s", bucket, key)


if __name__ == "__main__":
    main()

"""Unit tests for the campaign experiment framework.

These tests cover the two pieces that must be correct for causal lift claims to
be valid: (1) deterministic, balanced treatment/control assignment, and (2) the
statistical estimators used to judge whether a campaign worked.
"""

from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd
import pytest

# Make the src packages importable without installing the project.
SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from experiment.assign import assign_holdout, _assignment_fraction  # noqa: E402
from experiment.measure_lift import (  # noqa: E402
    build_outcome_frame,
    cuped_adjusted_lift,
    evaluate_experiment,
    minimum_detectable_effect,
    two_proportion_z_test,
)


def _scored_frame(n: int = 2000) -> pd.DataFrame:
    tokens = [f"tok_{i:06d}" for i in range(n)]
    segments = np.where(
        np.arange(n) % 3 == 0,
        "TARGET_PREMIUM",
        np.where(np.arange(n) % 3 == 1, "TARGET_STANDARD", "NURTURE"),
    )
    return pd.DataFrame({"customer_token": tokens, "campaign_segment": segments})


def test_assignment_is_deterministic():
    frame = _scored_frame()
    first = assign_holdout(frame, "campaign-A")
    second = assign_holdout(frame.sample(frac=1.0, random_state=1), "campaign-A")
    merged = first.merge(second, on="customer_token", suffixes=("_a", "_b"))
    # Group must not depend on row order.
    assert (merged["experiment_group_a"] == merged["experiment_group_b"]).all()


def test_assignment_changes_with_campaign_salt():
    frame = _scored_frame()
    a = assign_holdout(frame, "campaign-A").set_index("customer_token")["experiment_group"]
    b = assign_holdout(frame, "campaign-B").set_index("customer_token")["experiment_group"]
    # Different campaigns should reshuffle at least some customers.
    assert (a != b).any()


def test_control_fraction_is_approximately_honored():
    frame = _scored_frame(5000)
    assigned = assign_holdout(frame, "campaign-A", control_fraction=0.2)
    targeted = assigned[assigned["experiment_group"].isin(["treatment", "control"])]
    control_share = (targeted["experiment_group"] == "control").mean()
    assert control_share == pytest.approx(0.2, abs=0.03)


def test_non_targeted_segments_are_excluded():
    frame = _scored_frame(300)
    frame.loc[frame.index % 5 == 0, "campaign_segment"] = "EXCLUDE"
    assigned = assign_holdout(frame, "campaign-A")
    excluded = assigned[assigned["campaign_segment"] == "EXCLUDE"]
    assert (excluded["experiment_group"] == "not_targeted").all()


def test_assignment_fraction_in_unit_interval():
    values = [_assignment_fraction(f"tok_{i}", "c") for i in range(1000)]
    assert all(0.0 <= v < 1.0 for v in values)


def test_z_test_detects_real_lift():
    result = two_proportion_z_test(
        control_successes=30, control_total=1000, treatment_successes=80, treatment_total=1000
    )
    assert result["absolute_lift"] == pytest.approx(0.05, abs=1e-9)
    assert result["significant"] is True
    assert result["confidence_low"] < result["absolute_lift"] < result["confidence_high"]


def test_z_test_reports_no_significance_for_zero_effect():
    result = two_proportion_z_test(
        control_successes=30, control_total=1000, treatment_successes=31, treatment_total=1000
    )
    assert result["significant"] is False
    assert result["p_value"] > 0.05


def test_minimum_detectable_effect_shrinks_with_sample_size():
    small = minimum_detectable_effect(0.03, 200, 200)
    large = minimum_detectable_effect(0.03, 20000, 20000)
    assert large < small


def test_cuped_reduces_variance_without_biasing_estimate():
    rng = np.random.default_rng(0)
    n = 4000
    covariate = rng.normal(0, 1, n)
    group = np.where(rng.random(n) < 0.5, "treatment", "control")
    base = 0.1 + 0.05 * (covariate > 0)
    effect = np.where(group == "treatment", 0.04, 0.0)
    outcome = (rng.random(n) < np.clip(base + effect, 0, 1)).astype(int)
    frame = pd.DataFrame({"experiment_group": group, "adopted_card": outcome, "volume_30d": covariate})
    result = cuped_adjusted_lift(frame, "volume_30d")
    assert result is not None
    assert result["cuped_variance_reduction"] >= 0.0


def test_build_outcome_frame_supplies_campaign_segment():
    # Regression: assignments/labels carry no campaign_segment; it must come from
    # scores (or default to ALL) so evaluate_experiment does not raise.
    tokens = [f"tok_{i}" for i in range(100)]
    assignments = pd.DataFrame(
        {"customer_token": tokens, "experiment_group": ["treatment", "control"] * 50}
    )
    labels = pd.DataFrame({"customer_token": tokens, "adopted_card": [0, 1] * 50})

    # Without scores, segment defaults to ALL and evaluation still runs.
    outcome_no_scores = build_outcome_frame(assignments, labels)
    assert "campaign_segment" in outcome_no_scores.columns
    assert (outcome_no_scores["campaign_segment"] == "ALL").all()
    evaluate_experiment(outcome_no_scores)  # must not raise

    # With scores, the real segment is joined through.
    scores = pd.DataFrame(
        {"customer_token": tokens, "campaign_segment": ["TARGET_PREMIUM"] * 100}
    )
    outcome_with_scores = build_outcome_frame(assignments, labels, scores=scores)
    assert set(outcome_with_scores["campaign_segment"].unique()) == {"TARGET_PREMIUM"}


def test_build_outcome_frame_handles_duplicate_feature_partitions():
    # features_transactional accumulates one row per token per weekly partition;
    # the covariate merge must dedup rather than raise a many-to-one MergeError.
    tokens = [f"tok_{i}" for i in range(50)]
    assignments = pd.DataFrame(
        {"customer_token": tokens, "experiment_group": ["treatment", "control"] * 25}
    )
    labels = pd.DataFrame({"customer_token": tokens, "adopted_card": [0, 1] * 25})
    features = pd.concat(
        [
            pd.DataFrame({"customer_token": tokens, "volume_30d": 1.0, "txn_count_30d": 3}),
            pd.DataFrame({"customer_token": tokens, "volume_30d": 2.0, "txn_count_30d": 5}),
        ],
        ignore_index=True,
    )
    outcome = build_outcome_frame(assignments, labels, features=features)
    assert len(outcome) == len(tokens)


def test_evaluate_experiment_end_to_end():
    rng = np.random.default_rng(42)
    n = 6000
    group = np.where(rng.random(n) < 0.15, "control", "treatment")
    segment = rng.choice(["TARGET_PREMIUM", "TARGET_STANDARD", "NURTURE"], size=n)
    base = 0.03
    effect = np.where(group == "treatment", 0.05, 0.0)
    outcome = (rng.random(n) < base + effect).astype(int)
    frame = pd.DataFrame(
        {
            "experiment_group": group,
            "campaign_segment": segment,
            "adopted_card": outcome,
            "volume_30d": rng.normal(100, 20, n),
        }
    )
    scorecard = evaluate_experiment(frame)
    assert scorecard["overall"]["evaluable"] is True
    assert scorecard["overall"]["absolute_lift"] > 0
    assert len(scorecard["segments"]) == 3

"""Metric and model-card utilities for Visa card adoption propensity training."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score


def calculate_metrics(labels: np.ndarray, probabilities: np.ndarray) -> dict[str, float]:
    """Calculate ranking and calibration metrics for held-out binary labels."""
    if len(labels) == 0 or len(labels) != len(probabilities):
        raise ValueError("labels and probabilities must be non-empty arrays of equal length.")
    prevalence = float(np.mean(labels))
    top_count = max(1, int(np.ceil(0.10 * len(labels))))
    top_indices = np.argsort(probabilities)[-top_count:]
    precision_at_10 = float(np.mean(labels[top_indices]))
    metrics = {
        "pr_auc": float(average_precision_score(labels, probabilities)),
        "roc_auc": float(roc_auc_score(labels, probabilities)),
        "precision_at_10": precision_at_10,
        "lift_at_10": float(precision_at_10 / prevalence) if prevalence > 0 else 0.0,
        "brier_score": float(brier_score_loss(labels, probabilities)),
    }
    return metrics


def model_card(features: list[str], metrics: dict[str, float]) -> dict[str, Any]:
    """Build the versioned model card persisted beside the serialized estimator."""
    return {
        "model_name": "visa_card_adoption",
        "version": "1.0.0",
        "training_date": datetime.now(timezone.utc).isoformat(),
        "performance": {
            "pr_auc": metrics["pr_auc"],
            "precision_at_10": metrics["precision_at_10"],
            "lift_at_10": metrics["lift_at_10"],
            "brier_score": metrics["brier_score"],
        },
        "features_used": features,
        "limitations": [
            "Labels and transactions are synthetic and do not represent production customer behavior.",
            "Temporal validation uses repeated customer snapshots, so it does not measure population drift.",
            "Predictions support marketing prioritization and must not be used as a credit decision.",
        ],
        "refresh_cycle": "weekly",
    }


def write_model_card(path: Path, features: list[str], metrics: dict[str, float]) -> dict[str, Any]:
    """Write a model card JSON document and return the in-memory card."""
    card = model_card(features, metrics)
    path.write_text(json.dumps(card, indent=2, sort_keys=True), encoding="utf-8")
    return card


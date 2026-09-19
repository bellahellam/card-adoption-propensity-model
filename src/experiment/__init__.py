"""Experiment framework: randomized holdout assignment and causal lift measurement.

This package implements Level 1 of the digital-lift measurement capability:

- ``assign``: deterministic, stratified treatment/control assignment keyed on the
  existing ``customer_token`` so a campaign can hold back an untreated control group.
- ``measure_lift``: incremental-conversion (ATE) estimation with a two-proportion
  z-test, confidence intervals, minimum-detectable-effect power analysis, and
  optional CUPED variance reduction.

No raw PAN is ever handled here; the package operates only on tokens.
"""

from __future__ import annotations

__all__ = ["assign", "measure_lift"]

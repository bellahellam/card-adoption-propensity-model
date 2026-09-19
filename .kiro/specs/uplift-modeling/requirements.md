# Requirements Document

## Introduction

Level 2 (Uplift Modeling) extends the already-built Level 1 causal-inference framework of the Visa card-adoption propensity model. Where the Level 1 propensity model ranks customers by their *likelihood* to adopt, and the Level 1 lift-measurement framework proves whether a campaign *caused* adoption in aggregate, Level 2 estimates the **per-customer incremental effect** of the campaign — how much a specific customer's adoption probability increases *because* of being treated.

The feature trains a T-learner (two-model) uplift estimator that reuses the existing calibrated-XGBoost feature set and the treatment/control outcomes emitted by the Level 1 experiment. It evaluates ranking quality with uplift-specific metrics (Qini curve, Qini AUC, uplift-at-k) rather than propensity ranking metrics such as PR-AUC. It reshapes targeting output from propensity deciles into four actionable uplift cohorts — PERSUADABLE, SURE_THING, LOST_CAUSE, SLEEPING_DOG — so campaign teams can prioritize the customers whose behavior the campaign can actually change. Artifacts, a model card, and metrics are published following the same S3/MLflow/dbt conventions established in Level 1, and the new stage is wired into the existing Makefile and weekly GitHub Actions pipeline.

Level 1 is treated as an available upstream dependency. This document specifies only the incremental Level 2 behavior and consumes Level 1 outputs (assignments, labels, features, and the experiment mart) as inputs.

## Glossary

- **Uplift_Trainer**: The Level 2 component that fits the T-learner uplift model from treatment/control experiment outcomes joined to features. Corresponds to a new `src/uplift/train_uplift.py` entrypoint.
- **Uplift_Evaluator**: The Level 2 component that computes uplift-specific ranking metrics (Qini curve, Qini AUC, uplift-at-k) on a held-out slice of experiment data. Corresponds to a new `src/uplift/evaluate_uplift.py` module.
- **Cohort_Assigner**: The Level 2 component that maps each customer's predicted uplift and predicted baseline adoption probability to one of the four uplift cohorts.
- **Uplift_Scorer**: The Level 2 component that applies the trained uplift model to the latest feature snapshot and emits per-customer uplift scores and cohorts. Corresponds to a new `src/uplift/score_uplift.py` entrypoint.
- **T_Learner**: A two-model uplift estimator. One base model is fit on treatment-arm outcomes and one on control-arm outcomes; predicted uplift for a customer is the treatment-model probability minus the control-model probability.
- **Uplift_Score**: The estimated incremental adoption probability for a customer: `P(adopt | treated) - P(adopt | control)`. May be negative.
- **Qini_Curve**: A cumulative curve of incremental adoptions gained as customers are targeted in descending predicted-uplift order, used to assess uplift ranking quality.
- **Qini_AUC**: The area between the Qini_Curve and the random-targeting baseline, summarizing uplift ranking quality in a single scalar.
- **Uplift_At_K**: The cumulative incremental adoptions (or incremental adoption rate) captured within the top K fraction of customers ranked by predicted uplift.
- **Uplift_Cohort**: One of four targeting classes assigned per customer — `PERSUADABLE`, `SURE_THING`, `LOST_CAUSE`, `SLEEPING_DOG`.
- **PERSUADABLE**: A customer who adopts only when treated (high uplift). The primary target for campaign spend.
- **SURE_THING**: A customer who adopts whether or not treated (high baseline adoption, low uplift). Treatment is largely wasted spend.
- **LOST_CAUSE**: A customer who does not adopt whether or not treated (low baseline adoption, low uplift). Treatment has little effect.
- **SLEEPING_DOG**: A customer whose adoption probability *decreases* when treated (negative uplift). Should not be contacted.
- **Uplift_Model_Card**: A versioned JSON document describing the uplift model, its uplift metrics, features used, cohort thresholds, and limitations, mirroring the Level 1 model-card structure.
- **Uplift_Mart**: A dbt external Parquet mart exposing per-customer uplift scores and cohorts at customer grain for campaign consumption.
- **Feature_Columns**: The existing Level 1 feature set `[recency_days, txn_count_30d, volume_30d, digital_ratio_30d, cross_border_count_90d, age, tenure_months]`, reused unchanged by Level 2.
- **Experiment_Outcomes**: The joined dataset of Level 1 experiment assignments (`experiment_group`), observed labels (`adopted_card`), and Feature_Columns, at customer grain.
- **RUN_DATE**: The ISO-8601 date partition (`YYYY-MM-DD`) that drives deterministic, idempotent S3 partitioning across the pipeline.
- **Synthetic_Generator**: The Level 1 ingestion generator `src/ingestion/generate_and_tokenize.py`, which produces synthetic treatment/control outcomes with a heterogeneous `true_treatment_effect`.
- **Control_Fraction**: The share of the treated population held back as an untreated control group (Level 1 default 0.15).

## Requirements

### Requirement 1: Assemble Experiment Outcomes for Uplift Training

**User Story:** As an ML engineer, I want the uplift training data assembled by joining experiment assignments, observed outcomes, and the existing feature set, so that the uplift model learns from customers with known treatment/control status and known adoption results.

#### Acceptance Criteria

1. WHEN the Uplift_Trainer assembles Experiment_Outcomes, THE Uplift_Trainer SHALL join Level 1 experiment assignments, customer labels, and Feature_Columns on `customer_token`.
2. THE Uplift_Trainer SHALL retain only rows whose `experiment_group` is `treatment` or `control`.
3. WHERE a joined row is missing any value in Feature_Columns or is missing `adopted_card`, THE Uplift_Trainer SHALL exclude that row from Experiment_Outcomes.
4. THE Uplift_Trainer SHALL represent `adopted_card` as an integer outcome in `{0, 1}`.
5. IF Experiment_Outcomes contains zero `treatment` rows or zero `control` rows, THEN THE Uplift_Trainer SHALL terminate with a descriptive error identifying the empty arm.
6. THE Uplift_Trainer SHALL operate exclusively on `customer_token` values and SHALL NOT read, derive, or persist a raw PAN.

### Requirement 2: Train a T-Learner Uplift Model

**User Story:** As an ML engineer, I want a T-learner uplift model trained on the experiment outcomes reusing the calibrated-XGBoost feature set, so that the model estimates per-customer incremental campaign effect.

#### Acceptance Criteria

1. WHEN the Uplift_Trainer trains an uplift model, THE Uplift_Trainer SHALL fit one base classifier on the `treatment`-arm outcomes and one base classifier on the `control`-arm outcomes using Feature_Columns as inputs.
2. THE Uplift_Trainer SHALL reuse the existing calibrated-XGBoost configuration (weighted XGBoost with isotonic calibration) as the base learner for each arm.
3. WHEN computing an Uplift_Score for a customer, THE Uplift_Trainer SHALL compute the treatment-model adoption probability minus the control-model adoption probability.
4. IF either the `treatment` arm or the `control` arm contains fewer than two distinct outcome classes, THEN THE Uplift_Trainer SHALL terminate with a descriptive error identifying the affected arm.
5. THE Uplift_Trainer SHALL produce a serialized uplift model artifact that bundles both arm models and the Feature_Columns list.
6. THE Uplift_Trainer SHALL accept a fixed random seed so that repeated training on identical Experiment_Outcomes produces identical arm models.

### Requirement 3: Held-Out Evaluation Split

**User Story:** As an ML engineer, I want the uplift model evaluated on a held-out slice of experiment data that was not used for training, so that reported uplift metrics reflect generalization rather than fit.

#### Acceptance Criteria

1. WHEN the Uplift_Trainer prepares training data, THE Uplift_Trainer SHALL partition Experiment_Outcomes into a training slice and a held-out evaluation slice.
2. THE Uplift_Trainer SHALL assign each customer to exactly one of the training slice or the held-out evaluation slice.
3. THE Uplift_Trainer SHALL preserve both `treatment` and `control` rows within both the training slice and the held-out evaluation slice.
4. IF the held-out evaluation slice contains zero `treatment` rows or zero `control` rows, THEN THE Uplift_Trainer SHALL terminate with a descriptive error.
5. THE Uplift_Trainer SHALL assign a customer to the training slice or the held-out evaluation slice deterministically, so that repeated runs on identical Experiment_Outcomes produce identical slices.

### Requirement 4: Uplift-Specific Ranking Metrics

**User Story:** As a data scientist, I want ranking quality measured with uplift-specific metrics rather than propensity metrics, so that I evaluate the model on incremental effect instead of adoption likelihood.

#### Acceptance Criteria

1. WHEN the Uplift_Evaluator scores the held-out evaluation slice, THE Uplift_Evaluator SHALL rank customers in descending predicted Uplift_Score order.
2. THE Uplift_Evaluator SHALL compute a Qini_Curve from the ranked held-out treatment and control outcomes.
3. THE Uplift_Evaluator SHALL compute a Qini_AUC as the area between the Qini_Curve and the random-targeting baseline.
4. THE Uplift_Evaluator SHALL compute Uplift_At_K for at least one configurable top-fraction K.
5. THE Uplift_Evaluator SHALL NOT use PR-AUC or precision-at-k as the uplift ranking quality metric.
6. IF the held-out evaluation slice is empty, THEN THE Uplift_Evaluator SHALL terminate with a descriptive error.
7. WHEN a Qini_Curve is computed for a set of held-out outcomes, THE Uplift_Evaluator SHALL anchor the curve at zero cumulative incremental adoptions when zero customers are targeted.

### Requirement 5: Assign Uplift Cohorts

**User Story:** As a campaign manager, I want customers reshaped from propensity deciles into uplift cohorts, so that I can direct spend to customers whose adoption the campaign can actually change.

#### Acceptance Criteria

1. WHEN the Cohort_Assigner classifies a customer, THE Cohort_Assigner SHALL assign exactly one Uplift_Cohort from `{PERSUADABLE, SURE_THING, LOST_CAUSE, SLEEPING_DOG}`.
2. IF a customer's predicted Uplift_Score is below zero by more than a configured negative threshold, THEN THE Cohort_Assigner SHALL assign `SLEEPING_DOG`.
3. WHERE a customer's predicted Uplift_Score is at or above a configured positive uplift threshold, THE Cohort_Assigner SHALL assign `PERSUADABLE`.
4. WHERE a customer's predicted Uplift_Score is below the positive uplift threshold AND the predicted baseline adoption probability is at or above a configured baseline threshold, THE Cohort_Assigner SHALL assign `SURE_THING`.
5. WHERE a customer's predicted Uplift_Score is below the positive uplift threshold AND the predicted baseline adoption probability is below the configured baseline threshold AND the Uplift_Score is within the negative threshold of zero, THE Cohort_Assigner SHALL assign `LOST_CAUSE`.
6. THE Cohort_Assigner SHALL record the cohort thresholds used for a given assignment run.

### Requirement 6: Score Latest Features and Emit Uplift Output

**User Story:** As a campaign manager, I want the latest customer feature snapshot scored by the uplift model, so that current uplift scores and cohorts are available for the active campaign.

#### Acceptance Criteria

1. WHEN the Uplift_Scorer runs for a given RUN_DATE, THE Uplift_Scorer SHALL load the trained uplift model artifact and apply both arm models to the latest feature snapshot.
2. THE Uplift_Scorer SHALL emit, per customer, `customer_token`, predicted Uplift_Score, predicted baseline adoption probability, and assigned Uplift_Cohort.
3. THE Uplift_Scorer SHALL write output to a deterministic, idempotent Hive-partitioned S3 location keyed on RUN_DATE.
4. THE Uplift_Scorer SHALL write all S3 objects with SSE-S3 (AES256) server-side encryption.
5. WHEN the Uplift_Scorer is rerun for an identical RUN_DATE and identical inputs, THE Uplift_Scorer SHALL produce identical output.
6. IF the required feature columns are missing from the latest snapshot, THEN THE Uplift_Scorer SHALL terminate with a descriptive error listing the missing columns before writing any output object.

### Requirement 7: Publish Uplift Model Artifact and Model Card

**User Story:** As an ML engineer, I want the uplift model artifact and a model card published to S3, so that the model is versioned, auditable, and consumable by downstream scoring.

#### Acceptance Criteria

1. WHEN training completes, THE Uplift_Trainer SHALL upload the serialized uplift model artifact to a dedicated uplift S3 model prefix using SSE-S3 (AES256) before writing the Uplift_Model_Card.
2. WHEN the uplift model artifact has been uploaded successfully, THE Uplift_Trainer SHALL write an Uplift_Model_Card JSON document to a dedicated uplift S3 model prefix using SSE-S3 (AES256).
3. THE Uplift_Model_Card SHALL record the model name, a semantic version, the training date, the Feature_Columns used, the computed uplift metrics, and the cohort thresholds.
4. THE Uplift_Model_Card SHALL record a limitation stating that predictions support marketing prioritization and must not be used as a credit decision.
5. THE Uplift_Model_Card SHALL record a limitation stating that labels and treatment effects are synthetic and do not represent production customer behavior.
6. WHERE the uplift model artifact has been uploaded successfully, THE Uplift_Scorer SHALL treat the model as available for downstream scoring regardless of Uplift_Model_Card write status.

### Requirement 8: Log Uplift Metrics to MLflow

**User Story:** As a data scientist, I want uplift training runs logged to a dedicated MLflow experiment, so that uplift metrics are tracked separately from the propensity model.

#### Acceptance Criteria

1. WHEN an uplift training run completes, THE Uplift_Trainer SHALL log the run to an MLflow experiment that is distinct from the Level 1 `visa_card_adoption` experiment.
2. THE Uplift_Trainer SHALL log Qini_AUC and at least one Uplift_At_K value as MLflow metrics.
3. THE Uplift_Trainer SHALL log the training slice size, held-out evaluation slice size, and cohort thresholds as MLflow parameters.
4. THE Uplift_Trainer SHALL resolve the MLflow tracking URI from the `MLFLOW_TRACKING_URI` environment variable, defaulting to `sqlite:///mlflow.db`.
5. IF MLflow logging fails, THEN THE Uplift_Trainer SHALL record a warning and continue publishing the model artifact and model card.

### Requirement 9: Expose an Uplift-Cohort dbt Mart

**User Story:** As a campaign analyst, I want an uplift-cohort dbt mart at customer grain, so that I can query per-customer uplift scores and cohorts in SQL for campaign targeting.

#### Acceptance Criteria

1. THE Uplift_Mart SHALL materialize as a dbt external Parquet model following the existing partitioned-location convention.
2. THE Uplift_Mart SHALL expose one row per `customer_token` with the predicted Uplift_Score, predicted baseline adoption probability, and assigned Uplift_Cohort.
3. THE Uplift_Mart SHALL include a dbt test asserting that every `Uplift_Cohort` value is one of `{PERSUADABLE, SURE_THING, LOST_CAUSE, SLEEPING_DOG}`.
4. THE Uplift_Mart SHALL include a dbt test asserting that `customer_token` is unique and non-null.

### Requirement 10: Pipeline Integration

**User Story:** As a platform engineer, I want uplift training, scoring, and mart building wired into the existing Makefile and weekly GitHub Actions workflow, so that Level 2 runs automatically alongside the existing pipeline.

#### Acceptance Criteria

1. THE Makefile SHALL provide distinct targets that train the uplift model, score uplift, and build the Uplift_Mart.
2. WHEN the `run-pipeline` Makefile target executes, THE Makefile SHALL invoke the uplift training, uplift scoring, and Uplift_Mart targets after the Level 1 experiment steps.
3. THE weekly GitHub Actions workflow SHALL execute uplift training, uplift scoring, and Uplift_Mart building after the Level 1 scoring and experiment steps.
4. WHEN uplift training, scoring, and mart steps run, THE Makefile and workflow SHALL pass the RUN_DATE partition consistently with the Level 1 steps.
5. THE uplift CLI entrypoints SHALL resolve the S3 bucket from the `S3_BUCKET` environment variable or an equivalent `--bucket` argument, consistent with existing entrypoints.

### Requirement 11: Synthetic Data Sufficiency for Stable Training

**User Story:** As a POC maintainer, I want the synthetic generator tuned so the T-learner has sufficient positive labels in both arms, so that the demo trains stably despite the low base rate and control holdout.

#### Acceptance Criteria

1. WHERE the Synthetic_Generator is configured for the uplift demo, THE Synthetic_Generator SHALL produce enough control-arm positive labels for the T-learner control model to fit with both outcome classes present.
2. THE Synthetic_Generator SHALL preserve a heterogeneous `true_treatment_effect` so that PERSUADABLE and SURE_THING cohorts both exist in the generated population.
3. WHERE the control-arm positive count is insufficient for stable training, THE Synthetic_Generator SHALL support adjusting the Control_Fraction using the same configuration pattern established in Level 1.
4. THE Synthetic_Generator SHALL continue to derive treatment/control assignment deterministically from `customer_token`, consistent with the Level 1 assignment logic.

### Requirement 12: Unit Tests for Metrics and Cohort Logic

**User Story:** As an ML engineer, I want unit tests for the uplift metrics and cohort assignment logic, so that these calculations are verified independently of the pipeline.

#### Acceptance Criteria

1. THE test suite SHALL include a unit test asserting that Qini_AUC is zero (within tolerance) for a ranking with no better-than-random uplift ordering.
2. THE test suite SHALL include a unit test asserting that Qini_AUC is positive for a ranking that orders known-uplift customers ahead of known-non-uplift customers.
3. THE test suite SHALL include a unit test asserting that Uplift_At_K for the full population equals the overall incremental adoption count (within tolerance).
4. THE test suite SHALL include a unit test asserting that the Cohort_Assigner maps a customer with high positive Uplift_Score to `PERSUADABLE`.
5. THE test suite SHALL include a unit test asserting that the Cohort_Assigner maps a customer with Uplift_Score below the negative threshold to `SLEEPING_DOG`.
6. THE test suite SHALL include a unit test asserting that the Cohort_Assigner maps a low-uplift, high-baseline customer to `SURE_THING` and a low-uplift, low-baseline customer to `LOST_CAUSE`.

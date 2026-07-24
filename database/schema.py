"""
Every table definition and every default value in the system.

This module is the single place where the shape of the database is
declared. Nothing else issues DDL.

Two rules that the rest of the project depends on:

1. `DEFAULT_CONFIG` is a SEED, not a source of truth. It is inserted with
   INSERT OR IGNORE on first run and never again. Once a key exists in the
   `config` table, the database owns it and editing this file will not
   change it. That is what makes the running system configurable without
   a code change.

2. `SCHEMA_CONTRACT_ROWS` seeds the validation contract that stage 2
   (schema management) and stage 4 (data validation) read at runtime.
   The rules live in the database; the modules only enforce whatever
   they find there.
"""

import os

# ----------------------------------------------------------------------
# Tables
# ----------------------------------------------------------------------
# Order matters only for readability; SQLite has no dependency ordering
# requirement for these (no FK constraints are declared, deliberately —
# the lineage tables reference each other loosely so a partial pipeline
# run still records what it managed to complete).

TABLES = {
    # ------------------------------------------------------------------
    # Raw ingest. Written by the collector, never mutated by the pipeline.
    # ------------------------------------------------------------------
    "metrics": """
        CREATE TABLE IF NOT EXISTS metrics (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT NOT NULL,
            cpu_percent   REAL,
            mem_percent   REAL,
            mem_used_mb   REAL,
            disk_percent  REAL,
            disk_used_gb  REAL
        )
    """,

    # ------------------------------------------------------------------
    # Configuration. Every tunable value in the system lives here.
    # ------------------------------------------------------------------
    "config": """
        CREATE TABLE IF NOT EXISTS config (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            value_type  TEXT NOT NULL,
            category    TEXT,
            description TEXT,
            updated_at  TEXT
        )
    """,

    # Audit trail for config edits. Feeds lineage: a model can be traced
    # back to the exact configuration that produced it.
    "config_history": """
        CREATE TABLE IF NOT EXISTS config_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            key        TEXT NOT NULL,
            old_value  TEXT,
            new_value  TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            source     TEXT
        )
    """,

    # ------------------------------------------------------------------
    # Stage 2 / 4: the column contract. Validation rules, not code.
    # ------------------------------------------------------------------
    "schema_contract": """
        CREATE TABLE IF NOT EXISTS schema_contract (
            table_name  TEXT NOT NULL,
            column_name TEXT NOT NULL,
            dtype       TEXT NOT NULL,
            nullable    INTEGER NOT NULL DEFAULT 1,
            min_value   REAL,
            max_value   REAL,
            is_target   INTEGER NOT NULL DEFAULT 0,
            description TEXT,
            PRIMARY KEY (table_name, column_name)
        )
    """,

    # ------------------------------------------------------------------
    # Stage 3/5/6 output: the cleaned, regridded series the models train on.
    #
    # Holds the MEASURED series plus provenance labels — not the derived
    # columns from stage 6. Derived columns (deltas, EWMAs, volatility,
    # headroom) are a deterministic function of this table, so persisting
    # them would duplicate state and force a migration every time a
    # feature definition changes. Both the training path and the serving
    # path call `transform.add_derived()` on this data, which is what
    # keeps them from diverging.
    # ------------------------------------------------------------------
    "metrics_clean": """
        CREATE TABLE IF NOT EXISTS metrics_clean (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id        INTEGER NOT NULL,
            ts            TEXT NOT NULL,
            segment_id    INTEGER NOT NULL,
            cpu_percent   REAL,
            mem_percent   REAL,
            mem_used_mb   REAL,
            disk_percent  REAL,
            disk_used_gb  REAL,
            regime        TEXT,
            is_imputed    INTEGER NOT NULL DEFAULT 0,
            is_outlier    INTEGER NOT NULL DEFAULT 0,
            UNIQUE (run_id, ts)
        )
    """,

    # Retention tiers, long format so a new tier needs no schema change.
    "metrics_rollup": """
        CREATE TABLE IF NOT EXISTS metrics_rollup (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER NOT NULL,
            tier        TEXT NOT NULL,
            ts          TEXT NOT NULL,
            column_name TEXT NOT NULL,
            mean_value  REAL,
            max_value   REAL,
            p95_value   REAL,
            samples     INTEGER,
            UNIQUE (run_id, tier, ts, column_name)
        )
    """,

    # ------------------------------------------------------------------
    # Lineage: one row per ETL execution.
    # ------------------------------------------------------------------
    "pipeline_runs": """
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at        TEXT NOT NULL,
            finished_at       TEXT,
            source_kind       TEXT,
            source_detail     TEXT,
            rows_in           INTEGER,
            rows_out          INTEGER,
            gate_verdict      TEXT,
            gate_detail       TEXT,
            data_fingerprint  TEXT,
            config_fingerprint TEXT,
            status            TEXT,
            error             TEXT
        )
    """,

    # Individual validation check results, one row per check per run.
    "quality_checks": """
        CREATE TABLE IF NOT EXISTS quality_checks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id     INTEGER NOT NULL,
            check_name TEXT NOT NULL,
            status     TEXT NOT NULL,
            value      REAL,
            detail     TEXT,
            checked_at TEXT NOT NULL
        )
    """,

    # ------------------------------------------------------------------
    # Stage 10: feature store versioning.
    # ------------------------------------------------------------------
    "feature_versions": """
        CREATE TABLE IF NOT EXISTS feature_versions (
            version_id       TEXT PRIMARY KEY,
            target           TEXT NOT NULL,
            feature_list     TEXT NOT NULL,
            n_features       INTEGER,
            n_rows           INTEGER,
            config_fingerprint TEXT,
            data_fingerprint TEXT,
            run_id           INTEGER,
            created_at       TEXT NOT NULL
        )
    """,

    # Offline store: the materialised training rows.
    "feature_values": """
        CREATE TABLE IF NOT EXISTS feature_values (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id TEXT NOT NULL,
            ts         TEXT NOT NULL,
            segment_id INTEGER,
            features   TEXT NOT NULL,
            target_value REAL,
            UNIQUE (version_id, ts)
        )
    """,

    # Online store: the single latest feature vector per target, for serving.
    "feature_online": """
        CREATE TABLE IF NOT EXISTS feature_online (
            target     TEXT PRIMARY KEY,
            version_id TEXT NOT NULL,
            ts         TEXT NOT NULL,
            features   TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """,

    # ------------------------------------------------------------------
    # Stage 11 / 12: model registry, promotion, and inference log.
    # ------------------------------------------------------------------
    "model_versions": """
        CREATE TABLE IF NOT EXISTS model_versions (
            model_id           TEXT PRIMARY KEY,
            target             TEXT NOT NULL,
            algorithm          TEXT,
            params             TEXT,
            feature_version    TEXT,
            data_fingerprint   TEXT,
            config_fingerprint TEXT,
            run_id             INTEGER,
            train_rows         INTEGER,
            test_rows          INTEGER,
            mae                REAL,
            rmse               REAL,
            baseline_mae       REAL,
            improvement_pct    REAL,
            inference_ms       REAL,
            mlflow_run_id      TEXT,
            is_champion        INTEGER NOT NULL DEFAULT 0,
            promoted_at        TEXT,
            rejected_reason    TEXT,
            created_at         TEXT NOT NULL
        )
    """,

    # Every prediction made in serving. Actuals are backfilled once the
    # real value arrives, which is what makes drift measurable.
    "predictions": """
        CREATE TABLE IF NOT EXISTS predictions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            model_id    TEXT,
            target      TEXT NOT NULL,
            predicted_for_ts TEXT NOT NULL,
            predicted   REAL NOT NULL,
            actual      REAL,
            abs_error   REAL,
            created_at  TEXT NOT NULL,
            UNIQUE (target, predicted_for_ts, model_id)
        )
    """,

    # Drift monitor decisions.
    #
    # `action` is what the monitor DECIDED to do; `outcome` is what actually
    # happened once the retrain finished. They differ whenever a retrain ran
    # and the promotion gate then rejected the result — which is the normal
    # case on this data, and the thing an event log has to be able to say.
    "drift_events": """
        CREATE TABLE IF NOT EXISTS drift_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            target          TEXT NOT NULL,
            detected_at     TEXT NOT NULL,
            window_mae      REAL,
            reference_mae   REAL,
            ratio           REAL,
            threshold       REAL,
            action          TEXT,
            detail          TEXT,
            psi             REAL,
            trigger         TEXT,
            outcome         TEXT,
            new_model_id    TEXT,
            feature_version TEXT,
            resolved_at     TEXT
        )
    """,

    # ------------------------------------------------------------------
    # Recommendation output, so the cost story is queryable over time.
    # ------------------------------------------------------------------
    "recommendations": """
        CREATE TABLE IF NOT EXISTS recommendations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            target          TEXT NOT NULL,
            forecast_p95    REAL,
            wanted_percent  REAL,
            floored_percent REAL,
            allocated_units REAL,
            unit_label      TEXT,
            monthly_cost    REAL,
            static_cost     REAL,
            breach_rate     REAL,
            model_id        TEXT
        )
    """,
}

# Indexes that matter once the tables have real volume.
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts)",
    "CREATE INDEX IF NOT EXISTS idx_clean_run_ts ON metrics_clean(run_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_featvals_version ON feature_values(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_pred_target_ts ON predictions(target, predicted_for_ts)",
    "CREATE INDEX IF NOT EXISTS idx_model_target_champ ON model_versions(target, is_champion)",
    "CREATE INDEX IF NOT EXISTS idx_qc_run ON quality_checks(run_id)",
]


# ----------------------------------------------------------------------
# Column migrations
# ----------------------------------------------------------------------
# `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists,
# so a column added to a definition above never reaches a database created
# before the change. These are applied with ALTER TABLE ADD COLUMN, guarded
# by a check against PRAGMA table_info, so running them repeatedly is safe.
#
# Adding a column is the only migration shape allowed here. SQLite cannot
# drop or retype a column without rebuilding the table, and silently
# rebuilding a table that holds a user's measured history is not something
# an init path should ever do.
#
# (table, column, column_definition)

COLUMN_MIGRATIONS = [
    ("drift_events", "psi", "REAL"),
    ("drift_events", "trigger", "TEXT"),
    ("drift_events", "outcome", "TEXT"),
    ("drift_events", "new_model_id", "TEXT"),
    ("drift_events", "feature_version", "TEXT"),
    ("drift_events", "resolved_at", "TEXT"),
]


# ----------------------------------------------------------------------
# Config keys that must be present in `features.defining_keys`
# ----------------------------------------------------------------------
# `features.defining_keys` lists the settings that change what a feature
# VALUE means; the feature store hashes them into its version id. A key
# added to the system later has to join that list, or changing it would
# alter the features while leaving the version id — and therefore the
# train/serve compatibility check — unchanged.
#
# This is MERGED into whatever the database holds, never overwritten. A
# value the user has edited keeps every key they put there.

DEFINING_KEYS_REQUIRED = [
    "encoding.enabled",
    "encoding.method",
    "encoding.columns",
    "encoding.drop_first",
    "encoding.categories",
    "encoding.use_in_model",
]


# ----------------------------------------------------------------------
# Default configuration — seeded once, then owned by the database.
# ----------------------------------------------------------------------
# (key, value, value_type, category, description)

DEFAULT_CONFIG = [
    # ---- pricing: AWS us-east-1 on-demand, verified July 2026 ----------
    ("cost.vcpu_hour", "0.04048", "float", "pricing",
     "USD per vCPU-hour (AWS Fargate, us-east-1)"),
    ("cost.gb_ram_hour", "0.004445", "float", "pricing",
     "USD per GB-RAM-hour (AWS Fargate, us-east-1)"),
    ("cost.gb_storage_month", "0.08", "float", "pricing",
     "USD per GB-month (AWS EBS gp3, us-east-1)"),
    ("cost.hours_per_month", "730", "int", "pricing",
     "Standard cloud billing month in hours"),

    # ---- reference node capacity --------------------------------------
    ("node.vcpus", "8", "int", "node",
     "Total vCPUs on the reference node; utilisation % maps against this"),
    ("node.ram_gb", "32", "int", "node",
     "Total RAM in GB on the reference node"),
    ("node.storage_gb", "500", "int", "node",
     "Total provisioned storage in GB on the reference node"),

    # ---- collector -----------------------------------------------------
    ("collector.sample_interval_sec", "3", "int", "collector",
     "Seconds between samples taken by the psutil logger"),
    ("collector.disk_path", os.path.abspath(os.sep), "str", "collector",
     "Filesystem root measured for disk utilisation"),

    # ---- pipeline: source selection and cleaning -----------------------
    ("pipeline.default_source", "sqlite://", "str", "pipeline",
     "Which connector the pipeline reads from when none is named. "
     "sqlite:// | stream:// | csv://<path> | http://<url>"),
    ("pipeline.nominal_cadence_sec", "3", "int", "pipeline",
     "Expected sampling cadence; real cadence is detected at runtime"),
    ("pipeline.gap_multiple", "2.0", "float", "pipeline",
     "A time delta beyond this multiple of cadence is a COLLECTION GAP, "
     "not a slow sample. Feature windows may not straddle one."),
    ("pipeline.max_interpolate_run", "3", "int", "pipeline",
     "Longest run of consecutive missing samples we will interpolate"),
    ("pipeline.outlier_window", "11", "int", "pipeline",
     "Rolling window (samples) for Hampel outlier scoring"),
    ("pipeline.outlier_mad_threshold", "3.5", "float", "pipeline",
     "Median-absolute-deviation threshold (Iglewicz-Hoaglin default)"),
    ("pipeline.outlier_strategy", "flag_only", "str", "pipeline",
     "flag_only | interpolate | clip. CPU transitions here are real "
     "events, so the default flags without altering values."),
    ("pipeline.rollup_freqs", '["15s", "1min", "5min"]', "json", "pipeline",
     "Retention/downsample tiers produced by the transform stage"),
    ("pipeline.degenerate_std_threshold", "0.25", "float", "pipeline",
     "A target with std below this is constant; modelling it is not "
     "meaningful and the pipeline says so explicitly"),

    # ---- schema / validation ------------------------------------------
    ("validation.min_rows", "50", "int", "validation",
     "Fewer rows than this fails the gate — not enough to train on"),
    ("validation.max_null_fraction", "0.05", "float", "validation",
     "Null fraction above this in any target column fails the gate"),
    ("validation.max_duplicate_fraction", "0.02", "float", "validation",
     "Duplicate-timestamp fraction above this fails the gate"),
    ("validation.fail_on_warn", "false", "bool", "validation",
     "If true, WARN-level checks also block the pipeline"),

    # ---- regimes -------------------------------------------------------
    ("regime.bounds",
     '{"idle": [0.0, 15.0], "ramp": [15.0, 80.0], "saturated": [80.0, 100.1]}',
     "json", "regime",
     "CPU utilisation bands used to label operating regime"),

    # ---- stage 7: feature engineering ----------------------------------
    ("features.targets", '["cpu_percent", "mem_percent", "disk_percent"]',
     "json", "features", "Columns the system forecasts"),
    ("features.lags", "[0, 1, 3, 5, 10, 20]", "json", "features",
     "Lag depths in samples. Lag 0 is the CURRENT value — without it the "
     "model is solving a 2-step problem while calling it 1-step."),
    ("features.roll_window", "10", "int", "features",
     "Rolling window in samples for mean/std/max/min"),
    ("features.use_calendar", "true", "bool", "features",
     "Include hour/minute/dayofweek. Ablatable: on a single-hour dataset "
     "`minute` can memorise the load-generator phase."),
    ("features.use_interactions", "true", "bool", "features",
     "Include products/ratios of history-only features"),
    ("features.target_shift", "1", "int", "features",
     "How many steps ahead the supervised target sits"),
    ("features.defining_keys",
     '["features.lags", "features.roll_window", "features.use_calendar", '
     '"features.use_interactions", "features.target_shift", '
     '"pipeline.gap_multiple", "pipeline.max_interpolate_run"]',
     "json", "features",
     "Config keys that change what a feature VALUE means. Hashed into the "
     "feature-store version id so train and serve cannot silently diverge."),

    # ---- stage 6/7: categorical encoding --------------------------------
    # `regime` (idle / ramp / saturated) is the one non-numeric column the
    # system produces. Before encoding it existed only as a text label used
    # for error reporting, and the model never saw it at all.
    ("encoding.enabled", "true", "bool", "encoding",
     "Whether the analysis stage encodes categorical columns at all"),
    ("encoding.method", "onehot", "str", "encoding",
     "onehot | ordinal. One-hot makes no ordering claim and suits a tree. "
     "Ordinal is one column and assumes idle < ramp < saturated, which is "
     "true for regime but is an assumption the config should state."),
    ("encoding.columns", '["regime"]', "json", "encoding",
     "Categorical columns the encoder processes"),
    ("encoding.drop_first", "false", "bool", "encoding",
     "Drop the first one-hot column to avoid the dummy-variable trap. "
     "Only matters for linear models; a tree is unaffected, and keeping "
     "every level makes the encoded frame readable."),
    ("encoding.categories", "{}", "json", "encoding",
     "Explicit category vocabulary per column, e.g. "
     '{"regime": ["idle", "ramp", "saturated", "unknown"]}. Left empty, '
     "`regime` derives its vocabulary from the keys of regime.bounds. The "
     "vocabulary is FIXED and never read from the data: a serving row "
     "holding one regime must still produce every column training saw."),
    ("encoding.unknown_label", "unknown", "str", "encoding",
     "Category assigned to a null or unrecognised value. Always part of "
     "the vocabulary, so an unseen value cannot change the column set."),
    ("encoding.use_in_model", "true", "bool", "encoding",
     "Whether encoded columns are offered to the model as features. "
     "Ablatable: turning this off and retraining measures what the regime "
     "label is actually worth."),

    # ---- stage 8: feature selection ------------------------------------
    ("selection.corr_threshold", "0.95", "float", "selection",
     "Two features correlated above this are near-duplicates"),
    ("selection.min_variance", "0.000001", "float", "selection",
     "Variance below this makes a feature effectively constant"),
    ("selection.min_importance", "0.0001", "float", "selection",
     "Permutation importance at or below this contributes nothing"),
    ("selection.permutation_repeats", "10", "int", "selection",
     "Shuffles per feature when measuring permutation importance"),
    ("selection.enabled", "true", "bool", "selection",
     "Whether training uses the selected subset or all features"),

    # ---- stage 9: feature scaling --------------------------------------
    ("scaling.method", "standard", "str", "scaling",
     "standard | minmax | robust. Fitted on training rows only."),

    # ---- stage 11: model -----------------------------------------------
    ("model.algorithm", "gradient_boosting", "str", "model",
     "Estimator used for the champion candidate"),
    ("model.test_fraction", "0.2", "float", "model",
     "Chronological holdout fraction. Never shuffled — shuffling a time "
     "series leaks the future into training."),
    ("model.params",
     '{"n_estimators": 200, "max_depth": 3, "learning_rate": 0.05, '
     '"random_state": 42}',
     "json", "model", "Hyperparameters for the gradient booster"),
    ("model.ridge_alpha", "1.0", "float", "model",
     "Ridge penalty for the linear rung of the baseline ladder"),
    ("model.flat_threshold", "0.5", "float", "model",
     "Below this naive MAE the improvement percentage is noise; the "
     "pipeline reports absolute MAE instead"),
    ("model.forecast_horizon", "20", "int", "model",
     "Multi-step forecast length. 20 steps x 3s = 60s."),
    ("model.seasonal_lag", "80", "int", "model",
     "Load-generator period in samples (240s / 3s), for seasonal naive"),
    ("model.cv_folds", "4", "int", "model",
     "Rolling-origin CV folds. Never shuffled K-fold."),
    ("model.seed", "42", "int", "model", "Global random seed"),

    # ---- stage 11: tuning ----------------------------------------------
    ("tuning.grid",
     '{"n_estimators": [100, 200, 300, 400], "max_depth": [2, 3, 4, 5], '
     '"learning_rate": [0.01, 0.05, 0.1, 0.2], '
     '"subsample": [0.7, 0.85, 1.0], "min_samples_leaf": [1, 3, 5, 10]}',
     "json", "tuning", "Search space for randomised hyperparameter search"),
    ("tuning.n_iter", "30", "int", "tuning",
     "Randomised search budget per target"),
    ("tuning.enabled", "false", "bool", "tuning",
     "Whether the training run tunes or uses model.params directly"),

    # ---- stage 12: promotion gate --------------------------------------
    ("promotion.min_improvement_pct", "2.0", "float", "promotion",
     "A challenger must beat the champion by at least this much to be "
     "promoted. Prevents churn on noise."),
    ("promotion.require_beat_baseline", "true", "bool", "promotion",
     "A challenger that loses to the naive baseline is never promoted, "
     "however well it scores against the incumbent"),
    ("promotion.min_cv_folds_won", "0.75", "float", "promotion",
     "Fraction of rolling-origin CV folds a challenger must win against "
     "persistence. A single holdout can land on a favourable window: one "
     "model beat persistence by 10% on the holdout while losing 2 of 4 "
     "folds and scoring 16.2 vs 4.3 on the earliest one."),
    ("promotion.max_cv_std_ratio", "2.0", "float", "promotion",
     "Reject when the challenger's fold-to-fold MAE spread exceeds this "
     "multiple of the baseline's. An unstable model that averages well "
     "still fails unpredictably in production."),

    # ---- stage 12: drift -----------------------------------------------
    ("drift.window", "50", "int", "drift",
     "Recent predictions used for the rolling error measurement"),
    ("drift.min_samples", "30", "int", "drift",
     "Minimum scored predictions before drift can be declared"),
    ("drift.mae_ratio_threshold", "1.5", "float", "drift",
     "Rolling MAE this many times the training MAE triggers retraining"),
    ("drift.auto_retrain", "true", "bool", "drift",
     "Whether a drift event automatically launches a retrain"),
    ("drift.psi_threshold", "0.25", "float", "drift",
     "Population Stability Index above which the input distribution counts "
     "as majorly shifted. The conventional reading: <0.1 stable, 0.1-0.25 "
     "moderate, >0.25 major."),
    ("drift.retrain_on_feature_drift", "true", "bool", "drift",
     "Allow feature drift alone to trigger a retrain. Performance drift "
     "needs actuals, which arrive a horizon late and never arrive at all "
     "if the collector stops — so without this a system that has stopped "
     "being fed can never notice its inputs moved."),
    ("drift.retrain_cooldown_sec", "900", "int", "drift",
     "Minimum seconds between retrains of the same target. Drift persists "
     "until a better model is found, so without a cooldown a single "
     "sustained shift retrains on every single monitor cycle."),
    ("drift.categorical_columns", '["regime"]', "json", "drift",
     "Categorical columns monitored for distribution shift via their "
     "encoded form. A change in regime occupancy is exactly what a change "
     "of load-generator pattern looks like."),

    # ---- allocation policy ---------------------------------------------
    ("policy.headroom", "0.20", "float", "policy",
     "Safety buffer above the forecast peak"),
    ("policy.max_breach_rate", "5.0", "float", "policy",
     "SLA: maximum % of samples allowed to exceed the allocation"),
    ("policy.safety_window", "100", "int", "policy",
     "Recent actual samples consulted when computing the safety floor"),
    ("policy.sweep_start", "20.0", "float", "policy",
     "Lower bound of the cost-vs-SLA allocation sweep"),
    ("policy.sweep_stop", "100.0", "float", "policy",
     "Upper bound of the cost-vs-SLA allocation sweep"),
    ("policy.sweep_step", "0.5", "float", "policy",
     "Allocation increment in the cost-vs-SLA sweep"),

    # ---- retention ------------------------------------------------------
    ("retention.raw_days", "30", "int", "retention",
     "Age beyond which raw metrics may be purged"),
    ("retention.enabled", "false", "bool", "retention",
     "Whether the scheduler applies the retention policy"),
]


# ----------------------------------------------------------------------
# Stage 2 / 4: the column contract, seeded into `schema_contract`.
# ----------------------------------------------------------------------
# (table, column, dtype, nullable, min, max, is_target, description)

SCHEMA_CONTRACT_ROWS = [
    # Nullable on purpose: `id` is assigned by the database on insert, so
    # a CSV, HTTP or streaming source legitimately arrives without one.
    # Declaring it NOT NULL made every non-SQLite source fail the gate.
    ("metrics", "id", "int", 1, None, None, 0,
     "Autoincrement primary key; absent on ingest from external sources"),
    ("metrics", "ts", "datetime", 0, None, None, 0,
     "ISO-8601 timestamp; sorts correctly as text"),
    ("metrics", "cpu_percent", "float", 1, 0.0, 100.0, 1,
     "CPU utilisation percentage"),
    ("metrics", "mem_percent", "float", 1, 0.0, 100.0, 1,
     "Memory utilisation percentage"),
    ("metrics", "mem_used_mb", "float", 1, 0.0, None, 0,
     "Memory used in megabytes"),
    ("metrics", "disk_percent", "float", 1, 0.0, 100.0, 1,
     "Disk utilisation percentage"),
    ("metrics", "disk_used_gb", "float", 1, 0.0, None, 0,
     "Disk used in gigabytes"),
]

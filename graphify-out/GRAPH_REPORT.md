# Graph Report - .  (2026-07-22)

## Corpus Check
- Corpus is ~39,170 words - fits in a single context window. You may not need a graph.

## Summary
- 641 nodes · 1581 edges · 44 communities (32 shown, 12 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 40 edges (avg confidence: 0.76)
- Token cost: 253,559 input · 28,172 output

## Community Hubs (Navigation)
- Backtest & Cost Policies
- Menu & Dashboard Entry Points
- Baseline Ladder
- Feature Engineering
- Continuous Scheduler & Sources
- Config Access Layer
- Collection & CSV Export
- ETL Load & Run Lineage
- Data Cleaning
- MLflow Tracking & Promotion Gate
- SQL Layer & Champion Serving
- Validation & Gap Segmentation
- Schema Contract
- Drift Detection
- Dashboard Data Readers
- Connection & Table Definitions
- Transformation & Rollups
- Load Generator
- Config Seeding & Bootstrap
- Evidence Concepts
- Champion Resolution & Drift Check
- Data Source Interface
- Backtest Savings Claims
- Project Overview & Dependencies
- Regrid & Data Fingerprint
- Lag-Zero Framing Bug
- Mixed Cadence Regridding
- Segment Gap Walls
- SQL Injection Safety
- Evaluation Package
- Orchestration Package
- Pipeline Package
- Serving Package
- Tracking Package
- Chronological Split Rule
- Scaler Fit Rule
- Scaling Irrelevance Finding

## God Nodes (most connected - your core abstractions)
1. `query.execute_query()` - 77 edges
2. `run_pipeline.main (twelve stages)` - 47 edges
3. `get_int()` - 38 edges
4. `load_clean_frame()` - 36 edges
5. `train_one (train, evaluate, register)` - 28 edges
6. `get_json()` - 27 edges
7. `get_float()` - 26 edges
8. `build_features (supervised training table)` - 24 edges
9. `run (replay one target)` - 21 edges
10. `etl.run (stage 1-6 orchestrator)` - 19 edges

## Surprising Connections (you probably didn't know these)
- `DEFAULT_CONFIG is a seed, not a source of truth` --semantically_similar_to--> `Nothing Is Hardcoded (config table owns every tunable)`  [INFERRED] [semantically similar]
  database/schema.py → CLAUDE.md
- `CSV Round Trip (export then replay via csv:// source)` --semantically_similar_to--> `Walk-Forward Backtest Replay`  [INFERRED] [semantically similar]
  conversion/csv_export.py → CLAUDE.md
- `Quick Start / Run Commands` --references--> `psutil_logger.run_logger()`  [EXTRACTED]
  README.md → collector/psutil_logger.py
- `apply_safety_floor` --semantically_similar_to--> `check (drift decision)`  [INFERRED] [semantically similar]
  service/cost_model.py → serving/drift.py
- `Promotion Gate` --references--> `schema.DEFAULT_CONFIG — seed defaults`  [INFERRED]
  CLAUDE.md → database/schema.py

## Import Cycles
- 4-file cycle: `config.py -> crud/config_crud.py -> crud/query.py -> database/connection.py -> config.py`

## Hyperedges (group relationships)
- **Config read/write path: reader, cache, CRUD, SQL, seed** — config_typed_cached_reader, config_cache_invalidate, crud_config_crud_set_value, crud_query_execute_query, database_schema_default_config [EXTRACTED 1.00]
- **Collection loop: load generator drives CPU, logger samples, CRUD persists to metrics** — collector_load_generator_wave_pattern, collector_psutil_logger_run_logger, crud_metrics_crud_create_metric, crud_query_execute_query, database_schema_tables [EXTRACTED 1.00]
- **Evidence discipline: ladder, CV, gate, backtest, lineage fingerprints** — claudemd_baseline_ladder, claudemd_rolling_origin_cv, claudemd_promotion_gate, claudemd_backtest_replay, crud_config_crud_fingerprint [INFERRED 0.85]
- **Gap-awareness: detect, segment, and prove the contamination** — pipeline_validate_detect_cadence, pipeline_validate_find_gaps, pipeline_clean_segment_on_gaps, pipeline_clean_regrid, model_features_build_feature_frame, model_features_gap_blind_counter [INFERRED 0.85]
- **End-to-end lineage: data fingerprint -> feature version -> model registry** — pipeline_etl_data_fingerprint, pipeline_etl_run_lifecycle, model_feature_store_version_id, model_feature_store_materialise, model_forecast_register [INFERRED 0.85]
- **Anti-self-deception evaluation discipline** — model_baseline_run_ladder, model_features_rolling_origin_splits, model_forecast_cv_stability, model_forecast_score_champion, model_tuning_apply_best, model_scaling_leak_rationale [INFERRED 0.75]
- **Train -> log -> gate -> promote or fall back to baseline** — tracking_mlflow_tracker_log_training_run, tracking_mlflow_tracker_evaluate, tracking_mlflow_tracker_promote, tracking_mlflow_tracker_promote_baseline, tracking_mlflow_tracker_registry, serving_predictor_resolve_champion [EXTRACTED 1.00]
- **Predict -> log -> score -> drift -> retrain -> re-gate** — serving_predictor_predict, serving_predictor_log_prediction, serving_predictor_score, serving_predictor_predictions_table, serving_drift_check, serving_drift_retrain, tracking_mlflow_tracker_run_gate [EXTRACTED 1.00]
- **Forecast -> P95 + headroom -> safety floor -> price -> replay to verify** — serving_predictor_forecast_horizon, service_recommender_recommend_percent, service_cost_model_apply_safety_floor, service_cost_model_resource_monthly_cost, evaluation_backtest_run, evaluation_backtest_combined [EXTRACTED 1.00]

## Communities (44 total, 12 thin omitted)

### Community 0 - "Backtest & Cost Policies"
Cohesion: 0.05
Nodes (71): get_float(), backtest(), Cost & SLA tab, combined (whole-node aggregate), _fit (in-replay refit), _forecast_from_history, format_target(), _log() (+63 more)

### Community 1 - "Menu & Dashboard Entry Points"
Cohesion: 0.08
Nodes (64): get_json(), Live config editor tab, Streamlit dashboard (5 tabs), Baseline ladder panel, format_report(), run_all(), evidence_menu(), _frame() (+56 more)

### Community 2 - "Baseline Ladder"
Cohesion: 0.06
Nodes (52): best_baseline (strongest rung selector), drift(), Compare against the strongest baseline, not the handiest, mae(), naive_forecast(), persistence (next = current), persistence_lag1 (the old, two-step baseline), Baseline ladder — what the model has to beat.  A single "naive" number is easy t (+44 more)

### Community 3 - "Feature Engineering"
Cohesion: 0.07
Nodes (47): get_int(), ladder(), _add_calendar(), _add_interactions(), _add_lags(), _add_rolling(), build_feature_frame (single shared construction), build_features (supervised training table) (+39 more)

### Community 4 - "Continuous Scheduler & Sources"
Cohesion: 0.05
Nodes (32): apply_retention, scheduler.cycle (one continuous loop iteration), _handle_signal(), Incremental ETL via stream watermark, The continuous loop — what "continuously analyses" means in practice.      pytho, Purge raw rows older than the retention window., Run the psutil logger on a daemon thread., scheduler.run (the continuous loop) (+24 more)

### Community 5 - "Config Access Layer"
Cohesion: 0.06
Nodes (47): Full Prediction Lineage, psutil_logger.collect_metrics(), Read interval at call time, not as a default argument, all_values(), config cache + invalidate(), _crud(), feature_fingerprint(), fingerprint() (+39 more)

### Community 6 - "Collection & CSV Export"
Cohesion: 0.08
Nodes (45): psutil_logger.log_once(), psutil_logger.run_logger(), csv_export.export_all(), csv_export.export_clean(), csv_export.export_features(), csv_export.export_raw(), csv_export.export_rollup(), CSV export — the data plane in reverse.  Stage 1 defines a `CSVSource` that read (+37 more)

### Community 7 - "ETL Load & Run Lineage"
Cohesion: 0.15
Nodes (20): query.execute_insert(), Insert one row and return its new primary key.      `last_insert_rowid()` is sco, _bulk_insert(), A failed run still leaves a record, finish_run(), format_report(), load_clean / read_clean (metrics_clean), load_rollups (metrics_rollup long format) (+12 more)

### Community 8 - "Data Cleaning"
Cohesion: 0.13
Nodes (19): clean (8-step cleaning stage), clip_ranges(), deduplicate(), Flag outliers, do not delete them, flag_outliers(), Hampel outlier flagging (median + MAD), _hampel_scores(), impute (bounded segment-local interpolation) (+11 more)

### Community 9 - "MLflow Tracking & Promotion Gate"
Cohesion: 0.16
Nodes (17): Lineage chain (prediction -> model -> features -> data -> config), current_champion, evaluate (gate decision), init_mlflow(), log_training_run, promote, promote_baseline (persistence champion), Experiment tracking and the promotion gate.  Every training run is logged to MLf (+9 more)

### Community 10 - "SQL Layer & Champion Serving"
Cohesion: 0.17
Nodes (14): query.execute_many(), The single point where SQL is executed.  Every value is bound as a parameter. No, Insert many rows over one connection. Returns the row count.      A per-row `exe, load_model(), backfill (replay champion over history), Serving a baseline is a legitimate production state, _current_features(), Feature-version enforcement at serving (+6 more)

### Community 11 - "Validation & Gap Segmentation"
Cohesion: 0.17
Nodes (15): _gap_blind_row_count (contamination measure), Assign `segment_id`, incrementing at every break in collection.      This is the, segment_on_gaps (segment_id hard walls), _check(), detect_cadence (measured, not configured), find_gaps (collection breaks), PASS / WARN / FAIL three-verdict severity model, profile() (+7 more)

### Community 12 - "Schema Contract"
Cohesion: 0.17
Nodes (13): Coerce forgives, validate reports, Schema contract (database-held column rules), expected_columns(), STAGE 2 — Data Engineering: schema management.  The contract lives in the `schem, Check `df` against the contract. Returns a report dict.      Reports rather than, Return the declared column rules for `table`, in declaration order., Columns marked as forecast targets in the contract., Every numeric measurement column (everything but id and ts). (+5 more)

### Community 13 - "Drift Detection"
Cohesion: 0.18
Nodes (12): events(), feature_drift, psi (Population Stability Index), Drift detection and automatic retraining.  This is the "continuously analyses" c, Mean absolute error over the most recent scored predictions., Retrain and re-gate. The gate still decides what gets deployed., Population Stability Index between two distributions.      Conventional reading:, PSI between the earlier and most recent portions of the series. (+4 more)

### Community 14 - "Dashboard Data Readers"
Cohesion: 0.17
Nodes (6): clean_frame(), quality_checks(), Dashboard — the twelve-stage pipeline, visible.      streamlit run dashboard/app, targets(), Per-segment rows, span and NATIVE cadence.      Reported because a segment colle, segment_profile()

### Community 15 - "Connection & Table Definitions"
Cohesion: 0.18
Nodes (10): _configure(), connection.get_connection(), Initialise once per process, not per connection, Connection management and one-time database initialisation.  `get_connection()`, Pragmas that matter when a collector writes while a dashboard reads., Open a connection, initialising the database on first use., Force re-initialisation on the next connection.      Only needed by tests and by, reset_init_flag() (+2 more)

### Community 16 - "Transformation & Rollups"
Cohesion: 0.21
Nodes (10): build_rollups(), Rollups keep p95 and max, not just mean, normalise_units (MB->GB, percent->fraction), STAGE 6 — Data Transformation: standardisation, formatting, normalisation.  Clea, Make units explicit and consistent.      `mem_used_mb` alongside `disk_used_gb`, Downsample to `freq`, keeping mean, max and p95 per column.      Retaining only, Every retention tier named in config., Run every transformation step. Returns (frame, rollups, report). (+2 more)

### Community 17 - "Load Generator"
Cohesion: 0.24
Nodes (10): Load Generator Required For A Forecastable Signal, load_generator._burn(), main(), Windows multiprocessing __main__ guard, intensity = number of cores to stress (0 = idle).     Spawns that many burn pro, Generates a repeating wave of CPU load so the collector records     a forecasta, Fully occupy one CPU core for `duration` seconds., load_generator._spawn_load() (+2 more)

### Community 18 - "Config Seeding & Bootstrap"
Cohesion: 0.22
Nodes (11): Nothing Is Hardcoded (config table owns every tunable), Lazy _crud() import (circular-import avoidance), config.py — typed cached config reader, connection.init_db(), Create every table and index, then seed defaults. Idempotent., schema.SCHEMA_CONTRACT_ROWS — column contract, schema.DEFAULT_CONFIG — seed defaults, metrics.id declared nullable for non-SQLite sources (+3 more)

### Community 19 - "Evidence Concepts"
Cohesion: 0.22
Nodes (9): Baseline Ladder, Promotion Gate, Rolling-Origin Cross-Validation, stream_watermark.json — incremental ingest watermark, Derived columns are not persisted in metrics_clean, No FK constraints, deliberately (partial runs still record), schema.TABLES — every table definition, The Continuous Loop (+1 more)

### Community 20 - "Champion Resolution & Drift Check"
Cohesion: 0.29
Nodes (8): check (drift decision), The error the champion recorded when it was promoted., Decide whether `target` has drifted. Returns a decision dict., reference_error(), Two independent drift signals, The model currently approved to serve `target`., resolve_champion, BASELINE_PREFIX marker

### Community 21 - "Data Source Interface"
Cohesion: 0.33
Nodes (4): CSVSource, get_source URI factory, STAGE 1 — Data Sources.  Everything downstream reads through the `Source` interf, Build a Source from a URI-style spec.          get_source("sqlite://")         g

### Community 22 - "Backtest Savings Claims"
Cohesion: 0.40
Nodes (5): Walk-Forward Backtest Replay, The Measured SLA-Compliant Saving Is 2.52%, Allocation Safety Floor, CSV Round Trip (export then replay via csv:// source), Known Limits

### Community 23 - "Project Overview & Dependencies"
Cohesion: 0.40
Nodes (5): MLflow 3.x Requires A SQLite Backend, Predictive Resource Monitoring System, Twelve-Stage Pipeline, Bootstrap Paths (DB_PATH, DATA_DIR, MODEL_DIR, MLFLOW_URI), Pinned Dependency Set

### Community 24 - "Regrid & Data Fingerprint"
Cohesion: 0.40
Nodes (5): Regrid buckets rather than reindex-matching, Snap each segment onto an evenly spaced time axis.      Lag features are positio, regrid (resample-bucket onto uniform axis), data_fingerprint (content hash), Short stable hash of the input data.      Hashes CONTENT — row count, span, and

## Ambiguous Edges - Review These
- `seasonal_naive (load-generator period lag)` → `label_regimes (idle / ramp / saturated)`  [AMBIGUOUS]
  model/baseline.py · relation: conceptually_related_to
- `data_fingerprint (content hash)` → `Regrid buckets rather than reindex-matching`  [AMBIGUOUS]
  pipeline/etl.py · relation: conceptually_related_to

## Knowledge Gaps
- **11 isolated node(s):** `Twelve-Stage Pipeline`, `Baseline Ladder`, `Full Prediction Lineage`, `Pricing Basis (AWS us-east-1 on-demand)`, `Lazy _crud() import (circular-import avoidance)` (+6 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **12 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `seasonal_naive (load-generator period lag)` and `label_regimes (idle / ramp / saturated)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `data_fingerprint (content hash)` and `Regrid buckets rather than reindex-matching`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `query.execute_query()` connect `Collection & CSV Export` to `Backtest & Cost Policies`, `Menu & Dashboard Entry Points`, `Baseline Ladder`, `Continuous Scheduler & Sources`, `Config Access Layer`, `ETL Load & Run Lineage`, `MLflow Tracking & Promotion Gate`, `SQL Layer & Champion Serving`, `Validation & Gap Segmentation`, `Schema Contract`, `Drift Detection`, `Dashboard Data Readers`, `Connection & Table Definitions`, `Champion Resolution & Drift Check`, `Data Source Interface`?**
  _High betweenness centrality (0.183) - this node is a cross-community bridge._
- **Why does `get_int()` connect `Feature Engineering` to `Backtest & Cost Policies`, `Baseline Ladder`, `Continuous Scheduler & Sources`, `Config Access Layer`, `Collection & CSV Export`, `Data Cleaning`, `SQL Layer & Champion Serving`, `Validation & Gap Segmentation`, `Drift Detection`, `Champion Resolution & Drift Check`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **Why does `load_clean_frame()` connect `Menu & Dashboard Entry Points` to `Backtest & Cost Policies`, `Baseline Ladder`, `Feature Engineering`, `Continuous Scheduler & Sources`, `Collection & CSV Export`, `MLflow Tracking & Promotion Gate`, `SQL Layer & Champion Serving`, `Drift Detection`?**
  _High betweenness centrality (0.040) - this node is a cross-community bridge._
- **What connects `Twelve-Stage Pipeline`, `Baseline Ladder`, `Full Prediction Lineage` to the rest of the system?**
  _11 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Backtest & Cost Policies` be split into smaller, more focused modules?**
  _Cohesion score 0.05368382080710848 - nodes in this community are weakly interconnected._
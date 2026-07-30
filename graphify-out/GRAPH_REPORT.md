# Graph Report - MLOPSProject  (2026-07-31)

## Corpus Check
- 62 files · ~59,069 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 781 nodes · 1831 edges · 52 communities (37 shown, 15 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 40 edges (avg confidence: 0.75)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `25c9a781`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- get_int
- main.py
- baseline.py
- features.py
- sources.py
- config.py
- execute_query
- etl.py
- clean.py
- drift.py
- What every file does — in plain English
- validate.py
- pipeline/schema.py
- Kubernetes — three environments that cannot reach each other
- app.py
- apply_sql.py
- transform.py
- wave_pattern
- entrypoint.sh
- run.sh
- get_json
- config_crud.py
- execute_query
- run_twin.py
- execute_many
- features.lags Starts At 0
- Regrid Buckets, It Does Not Match
- Segment Boundaries Are Hard Walls
- metrics_crud.update_metric() column whitelist
- evaluation/__init__.py
- orchestration/__init__.py
- pipeline/__init__.py
- serving/__init__.py
- tracking/__init__.py
- Chronological Split, Never Shuffle
- Scalers Fit On Training Rows Only
- Scaling Is Irrelevant To The Champion
- forecast.py
- predictor.py
- run_logger
- tuning.py
- auth.py
- init_db
- forecast_horizon
- Promotion Gate

## God Nodes (most connected - your core abstractions)
1. `execute_query()` - 86 edges
2. `main()` - 47 edges
3. `get_int()` - 40 edges
4. `load_clean_frame()` - 38 edges
5. `get_json()` - 30 edges
6. `train_one()` - 28 edges
7. `get_float()` - 27 edges
8. `build_features()` - 24 edges
9. `run()` - 21 edges
10. `run()` - 20 edges

## Surprising Connections (you probably didn't know these)
- `DEFAULT_CONFIG is a seed, not a source of truth` --semantically_similar_to--> `Nothing Is Hardcoded (config table owns every tunable)`  [INFERRED] [semantically similar]
  database/schema.py → CLAUDE.md
- `CSV Round Trip (export then replay via csv:// source)` --semantically_similar_to--> `Walk-Forward Backtest Replay`  [INFERRED] [semantically similar]
  conversion/csv_export.py → CLAUDE.md
- `_forecast_from_history()` --semantically_similar_to--> `forecast_horizon()`  [INFERRED] [semantically similar]
  evaluation/backtest.py → serving/predictor.py
- `apply_safety_floor()` --semantically_similar_to--> `check()`  [INFERRED] [semantically similar]
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

## Communities (52 total, 15 thin omitted)

### Community 0 - "get_int"
Cohesion: 0.06
Nodes (61): get_float(), Cost & SLA tab, combined(), _fit(), _forecast_from_history(), format_target(), _log(), policy_oracle() (+53 more)

### Community 1 - "main.py"
Cohesion: 0.06
Nodes (83): get_json(), Live config editor tab, Streamlit dashboard (5 tabs), ladder(), Baseline ladder panel, format_report(), run_all(), evidence_menu() (+75 more)

### Community 2 - "baseline.py"
Cohesion: 0.06
Nodes (48): best_baseline(), drift(), Compare against the strongest baseline, not the handiest, mae(), naive_forecast(), persistence(), persistence_lag1(), Baseline ladder — what the model has to beat.  A single "naive" number is easy t (+40 more)

### Community 3 - "features.py"
Cohesion: 0.20
Nodes (13): _add_calendar(), _add_encoded(), _add_interactions(), _add_lags(), _add_rolling(), build_feature_frame(), STAGE 7 — Feature Engineering: derived, aggregated, date and interaction feature, Clock features.      Kept ablatable. On an hour-long dataset driven by a periodi (+5 more)

### Community 4 - "sources.py"
Cohesion: 0.07
Nodes (23): pipeline_menu(), CSVSource, describe_available(), export_csv(), get_source(), HTTPSource, Source as configuration, not code, STAGE 1 — Data Sources.  Everything downstream reads through the `Source` interf (+15 more)

### Community 5 - "config.py"
Cohesion: 0.07
Nodes (44): Full Prediction Lineage, all_values(), config cache + invalidate(), _crud(), feature_fingerprint(), fingerprint(), get(), get_category() (+36 more)

### Community 6 - "execute_query"
Cohesion: 0.09
Nodes (40): export_all(), export_clean(), export_features(), export_raw(), export_rollup(), CSV export — the data plane in reverse.  Stage 1 defines a `CSVSource` that read, _write(), delete_metric() (+32 more)

### Community 7 - "etl.py"
Cohesion: 0.15
Nodes (20): execute_insert(), Insert one row and return its new primary key.      `last_insert_rowid()` is sco, _bulk_insert(), data_fingerprint(), A failed run still leaves a record, finish_run(), load_clean(), load_rollups() (+12 more)

### Community 8 - "clean.py"
Cohesion: 0.11
Nodes (25): _gap_blind_row_count (contamination measure), Regrid buckets rather than reindex-matching, clean(), clip_ranges(), deduplicate(), Flag outliers, do not delete them, flag_outliers(), Hampel outlier flagging (median + MAD) (+17 more)

### Community 9 - "drift.py"
Cohesion: 0.11
Nodes (25): Lineage chain (prediction -> model -> features -> data -> config), BASELINE_PREFIX marker, current_champion(), ensure_portable_artifact_root(), evaluate(), _force_relative_location(), init_mlflow(), _is_unreachable() (+17 more)

### Community 10 - "What every file does — in plain English"
Cohesion: 0.12
Nodes (16): `collector/` — measuring the machine, `conversion/` — data in and out as files, `crud/` — talking to the database safely, `dashboard/` — seeing it all, `database/` — the shape of the storage, `evaluation/` — proving the saving is real, Generated folders (not source code), `model/` — building inputs and forecasting (+8 more)

### Community 11 - "validate.py"
Cohesion: 0.17
Nodes (15): _check(), detect_cadence(), find_gaps(), history(), PASS / WARN / FAIL three-verdict severity model, persist(), profile(), STAGE 4 — Data Validation.  Null checks, duplicate checks and business rules, (+7 more)

### Community 12 - "pipeline/schema.py"
Cohesion: 0.16
Nodes (15): coerce(), Coerce forgives, validate reports, contract(), expected_columns(), STAGE 2 — Data Engineering: schema management.  The contract lives in the `schem, Force `df` to match the declared types where possible.      Unparseable values b, Check `df` against the contract. Returns a report dict.      Reports rather than, Return the declared column rules for `table`, in declaration order. (+7 more)

### Community 13 - "Kubernetes — three environments that cannot reach each other"
Cohesion: 0.12
Nodes (16): 1. Its own data and configuration, 2. Its own image tag, 3. Its own port, Deploy, Kubernetes — three environments that cannot reach each other, Layout, NodePorts are not published to Windows, Prerequisite (+8 more)

### Community 14 - "app.py"
Cohesion: 0.11
Nodes (12): backtest(), clean_frame(), event_log(), get_current_usage(), latest_process_alerts(), quality_checks(), Dashboard — the twelve-stage pipeline, visible.      streamlit run dashboard/a, The absolute most recent raw metric from the collector. (+4 more)

### Community 16 - "transform.py"
Cohesion: 0.17
Nodes (14): add_derived(), build_rollups(), Rollups keep p95 and max, not just mean, label_regimes(), normalise_units(), STAGE 6 — Data Transformation: standardisation, formatting, normalisation.  Cl, Make units explicit and consistent.      `mem_used_mb` is converted to GB for, Downsample to `freq`, keeping mean, max and p95 per column.      Retaining onl (+6 more)

### Community 17 - "wave_pattern"
Cohesion: 0.20
Nodes (11): MLflow 3.x Requires A SQLite Backend, Nothing Is Hardcoded (config table owns every tunable), Predictive Resource Monitoring System, Twelve-Stage Pipeline, Bootstrap Paths (DB_PATH, DATA_DIR, MODEL_DIR, MLFLOW_URI), Lazy _crud() import (circular-import avoidance), config.py — typed cached config reader, schema.DEFAULT_CONFIG — seed defaults (+3 more)

### Community 20 - "get_json"
Cohesion: 0.20
Nodes (18): get_bool(), get_str(), all_encoded_columns(), category_shares(), encode(), encode_column(), encoded_columns(), format_report() (+10 more)

### Community 21 - "config_crud.py"
Cohesion: 0.10
Nodes (19): bullet(), kv(), legend(), note(), page(), The dashboard's visual system, in one place.  `.streamlit/config.toml` carries e, Level 1 — the tab's own title. Exactly one per tab., Level 2 — a rule-and-label divider between blocks of a tab. (+11 more)

### Community 22 - "execute_query"
Cohesion: 0.50
Nodes (4): apply(), Inject the stylesheet. Call once, immediately after set_page_config., Tint a table's rows by a status column, in light-surface tints.      The tint is, status_frame()

### Community 23 - "run_twin.py"
Cohesion: 0.25
Nodes (9): execute_many(), The single point where SQL is executed.  Every value is bound as a parameter. No, Insert many rows over one connection. Returns the row count.      A per-row `exe, get_connection(), Initialise once per process, not per connection, Open a connection, initialising the database on first use., init_twin_table(), main() (+1 more)

### Community 24 - "execute_many"
Cohesion: 0.21
Nodes (13): generate_cadence_drift(), generate_gap_injection(), generate_multi_host_shift(), generate_regime_change(), generate_sustained_spike(), main(), Vary sampling interval mid-run (e.g. 3s -> 5s -> 4s)., Same signal shape as regime_change but with a different baseline load (shifted b (+5 more)

### Community 44 - "forecast.py"
Cohesion: 0.50
Nodes (4): fit_scaler(), A scaler fitted on all rows leaks the test window, Fit a scaler on training rows. Returns a serialisable dict.      A plain dict ra, Stage 6 transformation is not stage 9 scaling

### Community 45 - "predictor.py"
Cohesion: 0.22
Nodes (15): load_model(), model_path(), backfill(), Serving a baseline is a legitimate production state, _current_features(), Feature-version enforcement at serving, forecast_horizon(), predict() (+7 more)

### Community 46 - "run_logger"
Cohesion: 0.13
Nodes (21): Load Generator Required For A Forecastable Signal, _burn(), main(), Windows multiprocessing __main__ guard, intensity = number of cores to stress (0 = idle).     Spawns that many burn pro, Generates a repeating wave of CPU load so the collector records     a forecasta, Fully occupy one CPU core for `duration` seconds., _spawn_load() (+13 more)

### Community 47 - "tuning.py"
Cohesion: 0.06
Nodes (45): get_int(), _gap_blind_row_count(), Rows the previous, segment-unaware construction would have kept., Expanding-window cross-validation for time series.      Each fold trains on ever, rolling_origin_splits(), importance_screen(), Permutation importance over built-in importances, Drop features whose shuffling does not measurably hurt the model. (+37 more)

### Community 49 - "auth.py"
Cohesion: 0.27
Nodes (10): _client(), _fetch_role(), _login_form(), logout(), Supabase-backed authentication for the dashboard.  Two audiences share one app:, Blocks the app until a customer or admin session exists.      Returns the sessio, One client per browser session, NOT one per process.      The obvious spelling h, The role is looked up server-side, never trusted from the form. (+2 more)

### Community 50 - "init_db"
Cohesion: 0.18
Nodes (11): apply_column_migrations(), _configure(), init_db(), merge_defining_keys(), Connection management and one-time database initialisation.  `get_connection()`, Create every table and index, then seed defaults. Idempotent., Pragmas that matter when a collector writes while a dashboard reads., Add columns declared after this database was first created.      `CREATE TABLE I (+3 more)

### Community 51 - "forecast_horizon"
Cohesion: 0.20
Nodes (10): Training-serving skew prevention via version id, build_serving_row(), One feature vector for the most recent moment, for inference.      Identical con, Train/serve consistency — one code path, horizon_summary(), predict_horizon(), Multi-step forecasting — now a thin wrapper over the serving path.  This module, The forecast trajectory as a plain list of values. (+2 more)

### Community 53 - "Promotion Gate"
Cohesion: 0.14
Nodes (14): Walk-Forward Backtest Replay, Baseline Ladder, The Measured SLA-Compliant Saving Is 2.52%, Promotion Gate, Rolling-Origin Cross-Validation, Allocation Safety Floor, CSV Round Trip (export then replay via csv:// source), stream_watermark.json — incremental ingest watermark (+6 more)

## Ambiguous Edges - Review These
- `seasonal_naive()` → `label_regimes()`  [AMBIGUOUS]
  model/baseline.py · relation: conceptually_related_to
- `data_fingerprint()` → `Regrid buckets rather than reindex-matching`  [AMBIGUOUS]
  pipeline/etl.py · relation: conceptually_related_to

## Knowledge Gaps
- **40 isolated node(s):** `run.sh script`, `Top level`, ``collector/` — measuring the machine`, ``conversion/` — data in and out as files`, ``crud/` — talking to the database safely` (+35 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **15 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `seasonal_naive()` and `label_regimes()`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `data_fingerprint()` and `Regrid buckets rather than reindex-matching`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `execute_query()` connect `execute_query` to `get_int`, `main.py`, `baseline.py`, `sources.py`, `config.py`, `etl.py`, `drift.py`, `validate.py`, `pipeline/schema.py`, `predictor.py`, `run_logger`, `app.py`, `tuning.py`, `run_twin.py`, `execute_many`?**
  _High betweenness centrality (0.171) - this node is a cross-community bridge._
- **Why does `get_int()` connect `tuning.py` to `get_int`, `main.py`, `baseline.py`, `features.py`, `config.py`, `clean.py`, `validate.py`, `predictor.py`, `run_logger`, `transform.py`?**
  _High betweenness centrality (0.054) - this node is a cross-community bridge._
- **Why does `get_connection()` connect `run_twin.py` to `execute_query`, `etl.py`, `app.py`, `run_logger`, `wave_pattern`, `init_db`, `execute_many`?**
  _High betweenness centrality (0.031) - this node is a cross-community bridge._
- **What connects `run.sh script`, `Top level`, ``collector/` — measuring the machine` to the rest of the system?**
  _40 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `get_int` be split into smaller, more focused modules?**
  _Cohesion score 0.06398809523809523 - nodes in this community are weakly interconnected._
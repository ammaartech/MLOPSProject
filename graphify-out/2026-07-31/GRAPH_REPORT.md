# Graph Report - MLOPSProject  (2026-07-31)

## Corpus Check
- 61 files · ~56,731 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 756 nodes · 1802 edges · 39 communities (24 shown, 15 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 40 edges (avg confidence: 0.75)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `e811e150`
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

## God Nodes (most connected - your core abstractions)
1. `execute_query()` - 85 edges
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
- `Quick Start / Run Commands` --references--> `run_logger()`  [EXTRACTED]
  README.md → collector/psutil_logger.py
- `apply_safety_floor()` --semantically_similar_to--> `check()`  [INFERRED] [semantically similar]
  service/cost_model.py → serving/drift.py
- `Load Generator Required For A Forecastable Signal` --rationale_for--> `wave_pattern()`  [EXTRACTED]
  CLAUDE.md → collector/load_generator.py

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

## Communities (39 total, 15 thin omitted)

### Community 0 - "get_int"
Cohesion: 0.06
Nodes (70): get_float(), get_int(), Cost & SLA tab, combined(), format_target(), _log(), policy_oracle(), policy_predictive() (+62 more)

### Community 1 - "main.py"
Cohesion: 0.06
Nodes (80): get_json(), Live config editor tab, Streamlit dashboard (5 tabs), Baseline ladder panel, _fit(), format_report(), Train on the data available at this point in the replay., run_all() (+72 more)

### Community 2 - "baseline.py"
Cohesion: 0.08
Nodes (31): ladder(), drift(), Compare against the strongest baseline, not the handiest, mae(), naive_forecast(), persistence(), persistence_lag1(), Baseline ladder — what the model has to beat.  A single "naive" number is easy t (+23 more)

### Community 3 - "features.py"
Cohesion: 0.05
Nodes (64): _forecast_from_history(), The champion's view of the next `steps` samples, from history only.      With a, best_baseline(), The strongest rung — what a model must actually beat., rmse(), Online store (refresh_online / get_online_features), Stable short id for one (target, feature definition, data) triple., Training-serving skew prevention via version id (+56 more)

### Community 4 - "sources.py"
Cohesion: 0.07
Nodes (24): pipeline_menu(), run_history(), CSVSource, describe_available(), export_csv(), get_source(), HTTPSource, Source as configuration, not code (+16 more)

### Community 5 - "config.py"
Cohesion: 0.05
Nodes (63): Full Prediction Lineage, Read interval at call time, not as a default argument, all_values(), config cache + invalidate(), _crud(), feature_fingerprint(), fingerprint(), get() (+55 more)

### Community 6 - "execute_query"
Cohesion: 0.06
Nodes (61): collect_metrics(), log_once(), Sample CPU, memory, and instantaneous disk I/O throughput.      Disk I/O is me, run_logger(), export_all(), export_clean(), export_features(), export_raw() (+53 more)

### Community 7 - "etl.py"
Cohesion: 0.05
Nodes (56): generate_cadence_drift(), generate_gap_injection(), generate_multi_host_shift(), generate_regime_change(), generate_sustained_spike(), main(), Vary sampling interval mid-run (e.g. 3s -> 5s -> 4s)., Same signal shape as regime_change but with a different baseline load (shifted b (+48 more)

### Community 8 - "clean.py"
Cohesion: 0.13
Nodes (19): _gap_blind_row_count (contamination measure), Regrid buckets rather than reindex-matching, clean(), clip_ranges(), deduplicate(), Flag outliers, do not delete them, Hampel outlier flagging (median + MAD), impute() (+11 more)

### Community 9 - "drift.py"
Cohesion: 0.05
Nodes (51): categorical_drift(), check(), events(), feature_drift(), in_cooldown(), psi(), Drift detection and automatic retraining.  This is the "continuously analyses" c, PSI between the earlier and most recent portions of the series. (+43 more)

### Community 10 - "What every file does — in plain English"
Cohesion: 0.12
Nodes (16): `collector/` — measuring the machine, `conversion/` — data in and out as files, `crud/` — talking to the database safely, `dashboard/` — seeing it all, `database/` — the shape of the storage, `evaluation/` — proving the saving is real, Generated folders (not source code), `model/` — building inputs and forecasting (+8 more)

### Community 11 - "validate.py"
Cohesion: 0.17
Nodes (15): _check(), detect_cadence(), find_gaps(), history(), PASS / WARN / FAIL three-verdict severity model, persist(), profile(), STAGE 4 — Data Validation.  Null checks, duplicate checks and business rules, (+7 more)

### Community 12 - "pipeline/schema.py"
Cohesion: 0.18
Nodes (13): coerce(), Coerce forgives, validate reports, contract(), expected_columns(), STAGE 2 — Data Engineering: schema management.  The contract lives in the `schem, Force `df` to match the declared types where possible.      Unparseable values b, Check `df` against the contract. Returns a report dict.      Reports rather than, Return the declared column rules for `table`, in declaration order. (+5 more)

### Community 13 - "Kubernetes — three environments that cannot reach each other"
Cohesion: 0.12
Nodes (16): 1. Its own data and configuration, 2. Its own image tag, 3. Its own port, Deploy, Kubernetes — three environments that cannot reach each other, Layout, NodePorts are not published to Windows, Prerequisite (+8 more)

### Community 14 - "app.py"
Cohesion: 0.09
Nodes (20): backtest(), clean_frame(), get_current_usage(), latest_process_alerts(), quality_checks(), Dashboard — the twelve-stage pipeline, visible.      streamlit run dashboard/a, Get the absolute most recent raw metric from the collector., targets() (+12 more)

### Community 16 - "transform.py"
Cohesion: 0.15
Nodes (16): Columns marked as forecast targets in the contract., target_columns(), add_derived(), build_rollups(), Rollups keep p95 and max, not just mean, label_regimes(), normalise_units(), STAGE 6 — Data Transformation: standardisation, formatting, normalisation.  Cl (+8 more)

### Community 17 - "wave_pattern"
Cohesion: 0.06
Nodes (35): Walk-Forward Backtest Replay, Baseline Ladder, Load Generator Required For A Forecastable Signal, The Measured SLA-Compliant Saving Is 2.52%, MLflow 3.x Requires A SQLite Backend, Nothing Is Hardcoded (config table owns every tunable), Predictive Resource Monitoring System, Promotion Gate (+27 more)

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
- **Why does `execute_query()` connect `execute_query` to `get_int`, `main.py`, `features.py`, `sources.py`, `config.py`, `etl.py`, `drift.py`, `validate.py`, `pipeline/schema.py`, `app.py`?**
  _High betweenness centrality (0.173) - this node is a cross-community bridge._
- **Why does `get_int()` connect `get_int` to `main.py`, `baseline.py`, `features.py`, `config.py`, `execute_query`, `clean.py`, `drift.py`, `validate.py`, `transform.py`?**
  _High betweenness centrality (0.056) - this node is a cross-community bridge._
- **Why does `load_clean_frame()` connect `main.py` to `get_int`, `baseline.py`, `features.py`, `execute_query`, `etl.py`, `drift.py`?**
  _High betweenness centrality (0.033) - this node is a cross-community bridge._
- **What connects `run.sh script`, `Top level`, ``collector/` — measuring the machine` to the rest of the system?**
  _40 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `get_int` be split into smaller, more focused modules?**
  _Cohesion score 0.05821917808219178 - nodes in this community are weakly interconnected._
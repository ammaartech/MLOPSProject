# Predictive Resource Monitoring System

MLOps internship project. Solo build.

## Problem statement

> Design a predictive resource monitoring system that continuously analyzes
> historical resource utilization patterns and forecasts future CPU, memory,
> and storage consumption. The system should recommend optimal resource
> allocation strategies to improve application performance and reduce
> infrastructure costs.

Four clauses, each with a home:

1. Continuously analyse historical utilisation -> `collector/`, `pipeline/`,
   `orchestration/scheduler.py`
2. Forecast CPU / memory / storage -> `model/`
3. Recommend allocation strategies -> `service/recommender.py`
4. Improve performance + reduce cost -> `service/cost_model.py`,
   `evaluation/backtest.py`

## Core idea

Watch usage -> forecast the next 60s -> recommend the smallest allocation
that stays inside the SLA -> price it against static over-provisioning ->
**replay the whole thing to check the number is real**.

---

## Environment

**Use the venv.** The system Python has none of the dependencies.

```powershell
.\.venv\Scripts\python.exe -m orchestration.run_pipeline
```

Run modules from the project root with `-m`, dotted, no extension.
`python model/forecast.py` breaks imports. Streamlit is the exception:
`streamlit run dashboard/app.py`.

---

## Architecture

```
config.py                   typed cached READER over the config table
database/schema.py          every table definition + seed defaults
database/connection.py      connect + one-time init (WAL enabled)
crud/                       query.py, metrics_crud.py, config_crud.py

pipeline/                   THE DATA PLANE — stages 1-6
  sources.py         [1]    SQLiteSource, CSVSource, HTTPSource, StreamSource
  schema.py          [2]    contract from schema_contract; coerce + validate
  etl.py             [3]    sequences 1-6, writes lineage, blocks on gate FAIL
  validate.py        [4]    13 checks -> PASS/WARN/FAIL; cadence + gap detection
  clean.py           [5]    dedupe, clip, SEGMENT ON GAPS, regrid, Hampel, impute
  transform.py       [6]    deltas, EWMA, trend, headroom, regimes, rollup tiers

model/                      FEATURES AND MODEL — stages 7-11
  features.py        [7]    gap-aware lags/rolling/interactions; lag_0 included
  selection.py       [8]    variance, correlation, permutation importance
  scaling.py         [9]    standard/minmax/robust, FIT ON TRAIN ROWS ONLY
  feature_store.py   [10]   offline + online stores, versioned, PIT join
  forecast.py        [11]   train, evaluate vs ladder, register with lineage
  tuning.py          [11]   randomised search, rolling-origin CV
  baseline.py               the six-rung baseline ladder
  horizon.py                thin wrapper over serving.predictor

tracking/                   MLOPS PLANE
  mlflow_tracker.py         MLflow logging + the promotion gate
  lineage.py                prediction -> model -> features -> data -> config

serving/                    STAGE 12 — deployment
  predictor.py              serves the champion; logs and scores predictions
  drift.py                  rolling error + PSI; triggers retrain

evaluation/backtest.py      walk-forward replay, five competing policies
service/cost_model.py       pricing, breach accounting (episodes, not just rate)
service/recommender.py      forecast -> headroom -> safety floor -> dollars

orchestration/
  run_pipeline.py           ALL twelve stages, one command
  scheduler.py              the continuous loop, optional in-process collector

dashboard/app.py            5 tabs, reads artifacts, EDITS config live
main.py                     menu covering everything above

Dockerfile                  two-stage build, python:3.12-slim
docker/entrypoint.sh        the Linux counterpart of run.bat, same verbs
docker-compose.yml          dashboard, mlflow, scheduler, collector, app
```

---

## Non-negotiables (DO NOT BREAK)

- **Nothing is hardcoded.** Every price, threshold, lag, SLA target and
  hyperparameter is a row in `config`. `config.py` reads; it does not
  define. The sole exception is `DB_PATH` (env `RESOURCE_MONITOR_DB`),
  which must exist before the table can be read. New settings go in
  `DEFAULT_CONFIG` in `database/schema.py`, which seeds with
  `INSERT OR IGNORE` and never overwrites a live value.

- **Segment boundaries are hard walls.** Every lag, rolling window and
  diff runs inside `groupby("segment_id")`. The data has three collection
  gaps, the longest 26.7 minutes. A gap-blind build keeps 366 rows; the
  correct one keeps 282.

- **`features.lags` starts at 0.** `lag_0` is the current value. Without
  it, features end at *t-1* while the target is *t+1* — a two-step
  problem labelled one-step. That framing is where the old "+15.2% over
  naive" came from.

- **Chronological split, never shuffle.** Rolling-origin CV for folds,
  never `KFold`. `RandomizedSearchCV` defaults to shuffled folds, which is
  why `tuning.py` implements its own loop.

- **Scalers fit on training rows only.**

- **Baselines evaluate on the FULL feature frame.** Selection can drop
  `lag_1` or `roll_mean`, which would silently delete ladder rungs.

- **Parameterized SQL only.** `update_metric` whitelists column names.

- **`execute_insert` for inserts needing the new id.** `execute_query`
  opens a connection per statement, so `last_insert_rowid()` on a second
  call always returns 0.

- **Use `is not None`, not truthiness, on MAE.** A perfect baseline scores
  0.0, which is falsy; treating that as "no baseline" let a worse model
  through the gate.

- **Regrid buckets, it does not match.** `reindex` onto a `date_range`
  silently discarded every off-grid timestamp — a third of the data. The
  series has mixed cadence (3s / 4s / 5s), so `resample` is required.

- **Rollups drop empty buckets.** `resample` spans the whole range, so a
  multi-day collection break produced 29,396 empty 15-second rows.

- **MLflow 3.x needs a SQLite backend.** `./mlruns` is gone. Use
  `sqlite:///data/mlflow.db`. `log_model(model, name=...)`;
  `artifact_path=` is deprecated.

- **The load generator is required for a forecastable CPU signal.** An
  idle laptop is flat.

- **Machine-specific config is resolved at read time, never written
  back.** `collector.disk_path` seeds from `os.path.abspath(os.sep)`, so
  a Windows-created database holds `C:\` and a Linux container cannot
  stat it. `resolve_disk_path()` substitutes the platform root for that
  reading only. `./data` is shared with the host, so "fixing" the stored
  value for one platform breaks the other.

- **MLflow artifact locations must stay relative.** MLflow resolves an
  experiment's `artifact_location` once, at creation, and stores it
  absolute. A Windows-created experiment read inside the container sent
  artifacts to `/C:/Users/...` in the container's writable layer, where
  they were discarded on exit — and nothing errored, because the metrics
  and params still reached the database. `ensure_portable_artifact_root()`
  rewrites an unreachable absolute location to `mlruns/<id>`, which
  resolves against the working directory and is therefore the same folder
  on both platforms.

---

## Current results

776 raw samples -> 808 cleaned rows, 6 segments, 682 usable feature rows
(a gap-blind build would have kept 787; 105 rows are contaminated).
Includes a 20-minute load-generator session: CPU 1.7%-100%, std 35.4.

### CPU baseline ladder

| Predictor | MAE |
|---|---|
| **MODEL (gradient boosting)** | **4.998** |
| persistence | 5.559 |
| ridge | 5.734 |
| persistence_lag1 (the old baseline) | 6.856 |
| drift | 9.575 |
| seasonal naive | 12.507 |
| rolling mean | 12.733 |

Memory: persistence 0.109, model 0.250 — flat.
Disk: persistence 0.000, model 0.000 — flat.

### The holdout win does not survive cross-validation

On the single chronological holdout the model beats persistence by
**+10.09%**. Fold by fold it does not:

| Fold | Train | Model | Persistence | Winner |
|---|---|---|---|---|
| 0 | 272 | **16.220** | 4.298 | persistence |
| 1 | 374 | 5.660 | 5.648 | persistence |
| 2 | 476 | 5.779 | 6.331 | model |
| 3 | 578 | 4.880 | 5.310 | model |

Model **8.135 +/- 4.681**; persistence **5.397 +/- 0.733**. Two folds each.

Fold 0 trains on the older idle data and is tested on load — the model
collapses to 16.2 where persistence barely moves. The model can win when
trained on enough same-regime data, but it is fragile across a regime
change and persistence is six times more stable.

### Promotion gate

All three challengers REJECTED; persistence baselines serve production.
CPU is rejected by the CV-stability criterion added because of exactly
the result above:

```
cpu_percent: REJECTED
  beat the baseline on the holdout but won only 2/4 CV folds
  (50%, threshold 75%). CV MAE 8.1349 vs baseline 5.3968.
  One favourable window is not evidence.
```

### Backtest (35 decisions, replayed)

| Policy | $/month | Worst breach | Saving | SLA |
|---|---|---|---|---|
| predictive, no floor | 264.01 | 39.00% | 30.57% | **no** |
| oracle | 275.44 | 0.00% | 27.56% | yes |
| predictive | 362.32 | 8.43% | 4.71% | **no** |
| **reactive P95 (no model)** | **370.66** | **4.14%** | **2.52%** | **yes** |
| static 100% | 380.24 | 0.00% | — | yes |

Savings fell (3.96% -> 2.52%) because the load generator made the machine
genuinely busy. Less idle capacity means less to reclaim — which is the
correct behaviour, not a regression.

---

## Headline claims

1. **The measured, SLA-compliant saving is 2.52%** — and it comes from the
   allocation policy, not from forecasting. The model-driven policy saves
   more only by breaching the SLA (8.43% against a 5% budget).
2. **A 10% accuracy win was caught as luck.** The gate rejects on
   cross-validated stability, not a single holdout.
3. **Full lineage**: any prediction traces to model, feature version, ETL
   run, data fingerprint, config fingerprint and the quality warnings that
   applied at training time.
4. Zero cloud spend — the whole stack runs on one laptop.

---

## Narrative for judges

- Most projects stop at forecasting. This closes the loop:
  predict -> decide -> price -> **replay to verify**.
- **Three times the measurement infrastructure caught its own project.**
  (a) The first version reported +15.2% over naive; the baseline ladder
  showed "naive" was measured two steps back. (b) With more data the model
  genuinely beat persistence by 10% on the holdout; rolling-origin CV
  showed it lost half the folds and scored 16.2 vs 4.3 on the earliest.
  (c) The gate compared a new model against a champion scored on
  different data, so incumbents are now re-scored on the current window.
- The promotion gate has teeth. It rejects all three models and serves
  measured baselines instead. A gate that has never rejected anything is
  decoration.
- The safety floor demonstrates the real cost/reliability tension:
  removing it saves 30.57% and breaches 39% of the time.
- **The model is not useless — it is unproven.** It wins on recent folds
  and loses across a regime change. That is a data-volume problem with a
  known fix, and the honest position is that it has not yet earned
  production.

---

## TODO

### Tier 1
1. **Collect much more data with the load generator running.** 14 backtest
   decisions is thin. Everything else is built; this is the binding
   constraint on every number in the project.
2. **Multi-pattern load generator** (bursty / step / sawtooth). Switching
   pattern mid-demo is what triggers drift live — the best demo moment
   available, and the drift path is already wired.

### Tier 2
3. Add a `host` column and a second collector, to answer "how does this
   scale to a fleet?" — `sources.HTTPSource` already exists for it.
4. Per-resource headroom shapes. The uniform `P95 x 1.20` asks for 102.8%
   on disk, clips to 100%, and discards the entire saving.
5. Slides + rehearse twice.

### Explicitly out of scope
- Autonomous self-healing. Off-brief and unprovable at this n.
- Bitbrains / Google Borg traces.
- FastAPI / Kubernetes. Surface area, not evidence.

Docker **was** on this list and is now built and published as
`ammaartech/predictive-resource-monitor`. It earned its place by
reproducing the host results exactly — same data and config
fingerprints, same champions, same MAEs — which turns "it works on my
laptop" into a checkable claim. It also surfaced two genuine portability
bugs; see the non-negotiables above.

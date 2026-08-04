# Predictive Resource Monitoring System

Watches CPU, memory and storage utilisation, forecasts the near future,
recommends the smallest allocation that stays inside an SLA, and prices
that allocation against static over-provisioning.

The output is a dollar figure with a breach rate attached, not just a
prediction.

---

## Quick start
## Start Guide
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# collect some data (two terminals)
.\.venv\Scripts\python.exe -c "from collector.psutil_logger import run_logger; run_logger()"
.\.venv\Scripts\python.exe -m collector.load_generator

# run everything: all twelve stages, ~40s
.\.venv\Scripts\python.exe -m orchestration.run_pipeline

# or run continuously, collector included
.\.venv\Scripts\python.exe -m orchestration.scheduler --with-collector

# see it
.\.venv\Scripts\python.exe -m streamlit run dashboard/app.py

# menu-driven equivalent of all of the above
.\.venv\Scripts\python.exe main.py
```

Every module also runs on its own: `python -m pipeline.validate`,
`python -m model.baseline`, `python -m evaluation.backtest`, and so on.

---

## Docker

Published as
[`ammaartech/predictive-resource-monitor`](https://hub.docker.com/r/ammaartech/predictive-resource-monitor)
(`1.0.0`, `latest`). No Python, no venv, no dependency install:

```powershell
docker compose up dashboard mlflow        # both UIs: :8501 and :5000
docker compose run --rm app pipeline      # all twelve stages, once
docker compose run --rm app drift         # drift monitor + auto-retrain
docker compose --profile live up          # the continuous scheduler
docker compose run --rm app shell         # a prompt inside the image
```

`run.bat docker-*` wraps the same commands for anyone who would rather
not remember compose syntax.

The container reproduces the host numbers exactly — same data
fingerprint `b5c281fb87b1`, same config fingerprint `e7ae4c10e9c4`, same
champions at the same MAEs. That is the point of containerising it: the
result is a property of the code and the data, not of one laptop.

### What is shared, and what that costs

`./dataset`, `./data` and `./mlruns` are bind-mounted, so `metrics.db`,
the models and the MLflow store are the same files the Windows tooling
uses. Runs from either side are visible to the other — which is precisely
why the fingerprints above match. Two consequences are worth knowing:

**`collector.disk_path` is machine-specific.** The config table seeds it
from `os.path.abspath(os.sep)`, so a database created on Windows stores
`C:\` — which does not exist in a Linux container.
`collector.psutil_logger.resolve_disk_path()` substitutes the platform
root at read time and deliberately does not write the substitute back;
correcting the shared value for one platform would break the other.

**MLflow artifact locations are resolved once, at experiment creation.**
An experiment created on Windows stored
`file:///C:/Users/.../mlruns/1`. Read from inside the container, MLflow
treated that as a local path and wrote artifacts to `/C:/Users/...` in
the container's own writable layer, where they vanished on exit —
silently, because the metrics and params still landed in the database
and only the artifacts disappeared.
`tracking.mlflow_tracker.ensure_portable_artifact_root()` rewrites such a
location to a relative `mlruns/<id>`, which resolves against the working
directory and so means the same physical folder on both platforms. Runs
logged before the migration keep their original absolute URIs.

### The collector is the one thing Docker cannot help with

`psutil` inside a Linux container reads `/proc`, which belongs to the
container and the Docker Desktop VM — **not** to Windows. The
`collector` service is there to demonstrate the path end to end; data you
intend to model still has to be collected on the host with
`run.bat collect`. This is a property of the platform, not a gap in the
image: no Linux container can measure a Windows host's CPU.

---

## The twelve stages

| #   | Stage               | Module                                                      |
| --- | ------------------- | ----------------------------------------------------------- |
| 1   | Data Sources        | `pipeline/sources.py` — SQLite, CSV, HTTP, streaming        |
| 2   | Data Engineering    | `pipeline/schema.py` — contract read from the database      |
| 3   | ETL Pipeline        | `pipeline/etl.py` — sequences 1-6, records lineage          |
| 4   | Data Validation     | `pipeline/validate.py` — 13 checks, PASS/WARN/FAIL          |
| 5   | Data Cleaning       | `pipeline/clean.py` — dedupe, gap-segment, regrid, impute   |
| 6   | Data Transformation | `pipeline/transform.py` — derive, normalise, roll up        |
| 7   | Feature Engineering | `model/features.py` — gap-aware lags, rolling, interactions |
| 8   | Feature Selection   | `model/selection.py` — variance, correlation, permutation   |
| 9   | Feature Scaling     | `model/scaling.py` — standard/minmax/robust, train-only fit |
| 10  | Feature Store       | `model/feature_store.py` — offline + online, versioned      |
| 11  | ML Model            | `model/forecast.py`, `model/tuning.py`, `model/baseline.py` |
| 12  | Deployment          | `tracking/`, `serving/` — gate, inference, drift            |

Plus `evaluation/backtest.py` for measured evidence and
`service/` for allocation and cost.

---

## Nothing is hardcoded

Every price, threshold, lag depth, SLA target and hyperparameter lives in
the `config` table. `config.py` is a typed, cached reader over it.

```python
from config import get_float, get_json
headroom = get_float("policy.headroom")
lags     = get_json("features.lags")
```

Change a value and the next run uses it — no code edit, no restart:

```python
config.set_value("policy.headroom", 0.10)
```

Every change is recorded in `config_history`, and the configuration is
hashed into a fingerprint that is stored with each trained model. The one
exception is the database path itself, which has to exist before the
table can be read; it comes from `RESOURCE_MONITOR_DB` or defaults to
`dataset/metrics.db`.

`dataset/` is tracked in git and `data/` is not, which is a deliberate
split rather than an oversight. `data/` holds output — the MLflow store,
the serialised models, the CSV exports — and re-running the pipeline
rebuilds all of it. `dataset/metrics.db` holds the collected samples, and
no amount of re-running brings back a measurement that was never taken.
Evidence is versioned; output is regenerated. A fresh clone therefore
arrives with the measurements already in it.

The dashboard's **Lineage & Config** tab edits these values live.

---

## What the measurements say

776 raw samples across three collection sessions, including 20 minutes
of load-generator waves driving CPU across its full 1.7%–100% range.

### The baseline ladder

CPU, every predictor on the identical test window:

| Predictor                                        | MAE       |
| ------------------------------------------------ | --------- |
| **GradientBoosting**                             | **4.998** |
| persistence (next = current)                     | 5.559     |
| ridge                                            | 5.734     |
| persistence_lag1 (next = value _before_ current) | 6.856     |
| drift (current + slope)                          | 9.575     |
| seasonal naive                                   | 12.507    |
| rolling mean                                     | 12.733    |

The model beats persistence by **10.09%** here. That result does not hold
up.

### The win is a lucky window

Rolling-origin cross-validation, the same comparison across four
expanding folds:

| Fold | Train rows | Model      | Persistence | Winner      |
| ---- | ---------- | ---------- | ----------- | ----------- |
| 0    | 272        | **16.220** | 4.298       | persistence |
| 1    | 374        | 5.660      | 5.648       | persistence |
| 2    | 476        | 5.779      | 6.331       | model       |
| 3    | 578        | 4.880      | 5.310       | model       |

Model **8.135 ± 4.681**. Persistence **5.397 ± 0.733**. Two folds each.

Fold 0 trains on the older idle data and is tested on load: the model
collapses to 16.2 while persistence barely notices. The model wins when
it has enough same-regime history and fails badly across a regime change.
Persistence is six times more stable.

Memory and disk are flat — persistence scores 0.109 and 0.000 — so there
is nothing to forecast at all.

### So the gate rejects every model

```
cpu_percent : REJECTED — beat the baseline on the holdout but won only
              2/4 CV folds (50%, threshold 75%). CV MAE 8.135 vs
              baseline 5.397. One favourable window is not evidence.
mem_percent : REJECTED — loses to persistence (0.250 vs 0.109)
disk_percent: REJECTED — baseline is exact; the model adds no improvement
```

Production serves the persistence baseline for all three. That is a
legitimate deployed state: the measured best predictor is the one
serving. Deploying a model that won once, because it happens to be the
one that got trained, is the failure the gate exists to prevent.

The CV-stability criterion was added _because_ of this result — a single
holdout had already passed a model that cross-validation shows is not
reliably better.

### The measured saving is 2.52%, from a policy with no model in it

Replaying 35 allocation decisions using only the data available at each
moment:

| Policy                      | $/month    | Worst breach | Saving    | SLA met |
| --------------------------- | ---------- | ------------ | --------- | ------- |
| predictive, no safety floor | 264.01     | 39.00%       | 30.57%    | **no**  |
| oracle (perfect foresight)  | 275.44     | 0.00%        | 27.56%    | yes     |
| predictive                  | 362.32     | 8.43%        | 4.71%     | **no**  |
| **reactive P95 (no model)** | **370.66** | **4.14%**    | **2.52%** | **yes** |
| static 100%                 | 380.24     | 0.00%        | —         | yes     |

Two things a snapshot cannot show:

- **The safety floor's price.** Removing it saves 30.57% and breaches
  39% of the time. The floor buys compliance by giving back most of the
  saving. That trade _is_ the finding.
- **Most of the value is in the policy, not the forecast.** A trailing
  P95 with no model reaches 2.52% and stays inside the SLA. The
  model-driven policy saves more only by breaching more.

The single-instant recommendation reports around 9%. The replay reports
2.52%. The replay is the number to trust, and `service/recommender.py`
says so in its own output.

---

## Findings from the data itself

**Collection gaps.** Three breaks, the longest 26.7 minutes. Every lag
and rolling window runs inside a `groupby("segment_id")`, so no feature
reaches across one. A gap-blind build keeps 366 rows; the gap-aware build
keeps 282. The 84-row difference is contaminated history.

**Mixed cadence.** 268 samples at 3s, 23 at 5s, 13 at 4s — the collector
interval changed mid-collection. `lag_20` therefore meant 100 seconds in
one part of the series and 60 in another. Regridding normalises this and
flags every synthetic row as `is_imputed`.

**A baseline measured two steps back.** The previous feature set began at
`lag_1` while the target was `shift(-1)`, so the "naive" prediction came
from the sample _before_ the one being predicted. That is a two-step
problem labelled as one step, and the gap between the two baselines was
being reported as model accuracy. `features.lags` now starts at 0 and
both rungs stay on the ladder so the difference is visible.

**Scaling is irrelevant to the champion, and measured to be.** MAE spread
across none/standard/minmax/robust: 0.004 for gradient boosting — trees
split on rank order — and 0.97 for ridge, whose penalty is scale
sensitive.

**Feature selection cost accuracy.** It cut 25 features to 14 and raised
MAE by 0.51 while inference was already 0.03 ms. `selection.enabled` is
therefore `false`, with the reason recorded in `config_history`.

---

## Lineage

Every prediction traces back to its inputs:

```
prediction -> model_id -> feature_version -> etl run_id
           -> data_fingerprint -> config_fingerprint -> quality_checks
```

```powershell
python -m tracking.lineage
```

prints, for each champion, the feature version it consumed, the ETL run
that produced its data, and the quality warnings that applied at the time
it was trained.

---

## The continuous loop

`orchestration/scheduler.py` runs the cycle that makes this a system
rather than a script:

```
collect -> incremental ETL -> refresh online features -> predict
        -> score against actuals -> drift check -> recommend -> price
```

Drift is watched two ways: rolling prediction error against the
champion's own recorded error, and population stability index on the
feature distribution. When performance degrades past
`drift.mae_ratio_threshold`, a retrain fires automatically — and the
retrained model faces the same gate as any other challenger.

Observed in a live run:

```
mem_percent  [DRIFTED]  rolling MAE 0.308 is 6.70x the reference 0.046
             -> action: retrain
RETRAIN OUTCOME (the gate still decides):
  mem_percent  REJECTED: loses to the 'persistence' baseline
```

Drift triggers a retrain. It does not authorise a deployment.

---

## Database

```
metrics            raw collector output, never mutated
metrics_clean      cleaned, regridded, segmented series
metrics_rollup     retention tiers (15s / 1min / 5min, mean+max+p95)
config             every tunable value in the system
config_history     audit trail of configuration changes
schema_contract    column rules that stages 2 and 4 enforce
pipeline_runs      one row per ETL execution, with fingerprints
quality_checks     per-check results, per run
feature_versions   feature definitions, hashed and versioned
feature_values     the offline store
feature_online     the online store, one vector per resource
model_versions     the registry: metrics, lineage, champion flag
predictions        every prediction, with actuals backfilled
drift_events       what drifted, when, and what was done
recommendations    allocation decisions and their prices
```

---

## Pricing basis

AWS us-east-1 on-demand, verified July 2026, stored in the `config`
table:

- Compute `$0.04048` / vCPU-hour (Fargate)
- Memory `$0.004445` / GB-hour (Fargate)
- Storage `$0.08` / GB-month (EBS gp3)

Reference node: 8 vCPU / 32 GB RAM / 500 GB. Full provisioning is
`$380.24`/month.

---

## Known limits

- **The backtest has 14 decision points.** With 387 cleaned samples and a
  20-step horizon there is not much room. The direction of the result is
  clear; the precision is not. More data is the single highest-value
  thing to add.
- **Only one host.** The schema has no `host` column, so the fleet
  question is unanswered.
- **The HTTP source has no endpoint.** The connector is real and works
  against any JSON endpoint returning a list of records; nothing in this
  project currently serves one.
- **CPU dynamics depend on the load generator.** An idle laptop produces
  a flat signal that nothing can forecast and nothing needs to.

---

## Recent Updates

- **Dynamic Capacity Monitor**: The Capacity Dashboard has been upgraded from static renderings to a dynamic, animated interface using `streamlit-autorefresh`. It now queries the `metrics` table in real-time, displaying live CSS-animated 3D cylinders.
- **SQL Ordering Fix**: Fixed a bug where the digital twin runs could not be read properly due to an incorrect `ORDER BY` clause.
- **Environment Consistency**: Fixed model pickling issues by ensuring models are trained and saved using the `.venv`'s native `scikit-learn` version (`1.9.0`), solving `ModuleNotFoundError` crashes during serving.

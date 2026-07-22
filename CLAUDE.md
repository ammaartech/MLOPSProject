# Predictive Resource Monitoring System

MLOps internship project. Solo build, ~4 days remaining.

## Problem statement

> Design a predictive resource monitoring system that continuously analyzes
> historical resource utilization patterns and forecasts future CPU, memory,
> and storage consumption. The system should recommend optimal resource
> allocation strategies to improve application performance and reduce
> infrastructure costs.

Four clauses, all of which must be visibly addressed:
1. Continuously analyze historical utilization -> collector + CRUD + SQLite
2. Forecast CPU / memory / storage -> `model/`
3. Recommend allocation strategies -> `service/recommender.py`
4. Improve performance + reduce cost -> `service/cost_model.py` + SLA guardrail

## Core idea

Watch resource usage -> forecast the next 60s -> recommend the smallest
allocation that stays safe -> price it against static over-provisioning.
The output is a dollar figure, not just a prediction.

## Architecture

```
config.py                 # ALL constants: paths, AWS prices, node capacity,
                          # headroom, horizon, hyperparameters, MLflow settings
main.py                   # menu-driven CLI (match/case)

database/connection.py    # SQLite conn + creates `metrics` table
crud/query.py             # execute_query(), PARAMETERIZED (no f-string SQL)
crud/metrics_crud.py      # create/read_all/read_latest/read_between/
                          # count/update/delete/purge_before

collector/psutil_logger.py    # samples CPU/mem/disk -> DB every N sec
collector/load_generator.py   # sine-wave CPU load across all cores

conversion/csv_export.py  # metrics table -> CSV

model/features.py         # time-series -> supervised table
                          # lags [1,3,5,10,20], rolling mean/std/max (win 10),
                          # hour/minute/dayofweek
model/baseline.py         # naive "next = current" predictor
model/forecast.py         # GradientBoosting per target, CHRONOLOGICAL split,
                          # evaluates vs baseline, saves joblib, predict_next()
model/horizon.py          # iterative multi-step forecast (20 steps = 60s)

service/cost_model.py     # utilization % -> provisioned units -> dollars
service/recommender.py    # forecast P95 -> +headroom -> SAFETY FLOOR -> cost

tracking/mlflow_tracker.py  # MLflow logging + champion/challenger gating

dashboard/app.py          # Streamlit, 5s auto-refresh
data/                     # metrics.db, models/, mlflow.db
```

## Schema

```sql
metrics(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,      -- ISO, sorts correctly as text
    cpu_percent  REAL,
    mem_percent  REAL,
    mem_used_mb  REAL,
    disk_percent REAL,
    disk_used_gb REAL
)
```

## Current results

Data: 287 samples @ 3s intervals, with load-generator sine waves (0-100% CPU).

Forecasting:

| Resource | Model MAE | Naive MAE | Verdict            | Inference |
|----------|-----------|-----------|--------------------|-----------|
| CPU      | 5.251     | 6.193     | +15.2% beats naive | 0.013 ms  |
| Memory   | 0.308     | 0.072     | flat, naive wins   | 0.012 ms  |
| Disk     | 0.000     | 0.000     | flat, nothing to predict | 0.013 ms |

Recommendation + cost:

| Resource | Forecast P95 | Forecast wanted | Safety-floored | Breaches |
|----------|--------------|-----------------|----------------|----------|
| CPU      | 47.45%       | 56.94%          | 91.94%         | 5.0%     |
| Memory   | 68.64%       | 82.37%          | 82.37%         | 0.0%     |
| Disk     | 85.70%       | 100%            | 100%           | 0.0%     |

Static (100% provisioned): $380.24/mo
Predictive allocation:     $342.88/mo
Savings:                   $37.36/mo (9.8%) at <=5% SLA breach

## Key decisions & gotchas (DO NOT BREAK THESE)

- **Chronological split, never shuffle.** Time-series shuffling leaks the
  future into training. `chronological_split()` in forecast.py.
- **Parameterized SQL only.** No f-string interpolation of user values.
  `update_metric()` whitelists column names.
- **Flat-signal handling.** When naive MAE < 0.5 the improvement percentage
  explodes (was showing -326%). We report absolute MAE instead. This is
  deliberate and is part of the honest-tradeoff narrative.
- **Safety floor is intentional.** The forecast wants CPU at 56.94% but that
  breaches 35% of the time, so the floor raises it to 91.94% for <=5%.
  This tension IS the finding, not a bug.
- **MLflow 3.x deprecated the file-based `./mlruns` backend.** Must use a
  SQLite backend: `sqlite:///data/mlflow.db`. Most online tutorials are stale.
- **`log_model(model, name="model")`** is the MLflow 3.x signature
  (`artifact_path=` is deprecated). Code has a try/except fallback for 2.x.
- **Laptop data is flat when idle.** The load generator is required to
  produce a forecastable signal. Run it alongside the logger.
- **Windows + spaces in path.** Project lives in a folder with a space
  ("MLOPS Project") - quote paths in shell commands.
- **Run modules from project root with `-m`**, dotted, no extension:
  `python -m model.forecast`. Running `python model/forecast.py` breaks
  imports. Streamlit is the exception: `streamlit run dashboard/app.py`.

## Pricing basis

AWS us-east-1, on-demand, verified July 2026:
- Compute:  $0.04048 / vCPU-hour   (Fargate)
- Memory:   $0.004445 / GB-hour    (Fargate)
- Storage:  $0.08 / GB-month       (EBS gp3)

Reference node modeled: 8 vCPU / 32 GB RAM / 500 GB storage.

## Commands

```powershell
# collect (run both together, separate terminals)
python -c "from collector.psutil_logger import run_logger; run_logger(interval=3)"
python -m collector.load_generator

# train + evaluate
python -m model.forecast
python -m model.horizon

# recommend
python -m service.cost_model
python -m service.recommender

# tracking
python -m tracking.mlflow_tracker
mlflow ui --backend-store-uri sqlite:///data/mlflow.db

# dashboard
streamlit run dashboard/app.py

# export
python -m conversion.csv_export

# sanity check
python -c "from crud.metrics_crud import count_metrics; print(count_metrics())"
```

## TODO (priority order)

### Tier 1 - must do
1. **Run + verify MLflow.** Code written, never executed. Run twice; the
   second pass should print REJECTED, proving the promotion gate works.
2. **Backtest / replay.** THE BIGGEST EVIDENCE GAP. The 9.8% figure is a
   single snapshot from one instant. Replay all 287 samples, make an
   allocation decision at every step, accumulate cost + breach rate vs
   static. Produces a chart and a defensible measured result.
3. **Drift detection + auto-retrain.** Track rolling forecast error; when it
   degrades past a threshold, retrain automatically and let the promotion
   gate decide deployment. This is the "continuously" clause and the main
   thing separating MLOps from a notebook.

### Tier 2 - high impact
4. **Multi-pattern load generator** (bursty / step / sawtooth). Model
   currently only knows "smooth sine". Switching patterns mid-demo is what
   TRIGGERS drift live - best demo moment available.
5. **Cost-vs-SLA tradeoff curve.** Sweep allocation levels, plot cost against
   breach rate, mark the knee. Turns the CPU tension into the headline finding.
6. **Wire champion into serving.** `predict_next()` and `horizon.py` load
   `{target}.joblib`, NOT `{target}_champion.joblib`. Promotion gating
   currently decides nothing.

### Tier 3 - polish
- Add Forecast/Recommend options to `main.py` (menu is incomplete)
- Add `host` column to schema (answers "how does this scale to a fleet?")
- Orchestrator: one command starts collector + scheduler + drift monitor
- Data quality gate before training (row count, nulls, >100% values, gaps)
- Wire `purge_before()` to a retention schedule (written, never called)
- Pin `requirements.txt` versions
- README + slides + rehearse twice

### Explicitly out of scope
- Autonomous self-healing (killing processes, restarting Docker, deleting
  files). Off-brief, unprovable with n=3 interventions, risks breaking the
  dev machine days before demo.
- Bitbrains / Google Borg traces. Nice-to-have; not worth the wrangling time.
- FastAPI / Docker / Kubernetes. Surface area, not evidence.

## Headline claims

1. +15.2% forecast accuracy over naive baseline (CPU) at 0.013 ms/prediction
2. 9.8% infrastructure cost reduction while holding SLA breaches <=5%
3. Zero cloud spend - entire stack runs on one laptop

## Narrative for judges

- Most projects stop at forecasting. This closes the loop:
  predict -> decide -> price -> validate.
- Honest tradeoff: CPU has exploitable dynamics so the model beats naive by
  15%; memory and disk were stable so naive is already near-optimal. Knowing
  when a model adds nothing is itself an engineering result.
- The safety floor demonstrates the real cost/reliability tension: the
  forecast wanted a lean 57% allocation, the SLA constraint forced 92%.

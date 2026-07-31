# How the system works, end to end

A walkthrough of one number's journey: from a CPU reading taken by
`psutil`, through twelve pipeline stages, to a dollar figure on the
dashboard — and back again, because every number can be traced to the
data, model and configuration that produced it.

This is the explanation document. `README.md` is the pitch, `FILES.md` is
the file-by-file map, and `CLAUDE.md` is the working brief. This one
answers "what actually happens, and which function does it".

---

## Contents

1. [The shape of the thing](#1-the-shape-of-the-thing)
2. [Foundations: config and the database](#2-foundations-config-and-the-database)
3. [How data gets in](#3-how-data-gets-in)
4. [Stages 1–6: the data plane](#4-stages-16-the-data-plane)
5. [Stages 7–10: features](#5-stages-710-features)
6. [Stage 11: training, and the ladder it must climb](#6-stage-11-training-and-the-ladder-it-must-climb)
7. [Stage 12: the gate, serving, and drift](#7-stage-12-the-gate-serving-and-drift)
8. [Turning a forecast into money](#8-turning-a-forecast-into-money)
9. [The process alert](#9-the-process-alert)
10. [The synthetic twin](#10-the-synthetic-twin)
11. [Running it: one pass, or forever](#11-running-it-one-pass-or-forever)
12. [The dashboard](#12-the-dashboard)
13. [Lineage: closing the loop](#13-lineage-closing-the-loop)
14. [Function reference](#14-function-reference)

---

## 1. The shape of the thing

The brief was: *forecast CPU / memory / storage, recommend an allocation,
improve performance and cut cost.* Most projects stop after the forecast.
This one carries on:

```
   watch  ->  forecast  ->  decide  ->  price  ->  REPLAY TO VERIFY
```

That last step is the one that matters. A forecast that looks accurate
and an allocation that looks cheap are both easy to produce; the
walk-forward backtest re-makes every decision using only the data that
existed at the time, then charges for what happened next. It is the
difference between a projected saving and a measured one.

The system is built as twelve numbered stages. They map to directories:

| Stages | What happens | Where |
|---|---|---|
| 0 | configuration | [config.py](config.py) |
| 1–6 | the data plane: extract, conform, validate, clean, transform, load | [pipeline/](pipeline/) |
| 7–10 | features: engineer, select, scale, store | [model/](model/) |
| 11 | train and evaluate against baselines | [model/forecast.py](model/forecast.py) |
| 12 | gate, serve, watch for drift | [tracking/](tracking/), [serving/](serving/) |
| — | evidence: replay and price | [evaluation/](evaluation/), [service/](service/) |

---

## 2. Foundations: config and the database

Two rules hold the whole thing together, and almost every design decision
downstream follows from them.

### Nothing is hardcoded

Every price, threshold, lag depth, SLA target and hyperparameter is a
**row in the `config` table**. [config.py](config.py) is a *reader*; it
does not define values.

```python
headroom = config.get_float("policy.headroom")     # 0.20
lags     = config.get_json("features.lags")        # [0, 1, 3, 5, 10, 20]
```

- **`get()`** reads a key and coerces it to the type declared in its row,
  raising `KeyError` if it is missing and no default was given. A silent
  `None` here would surface much later as a confusing failure inside a
  cost calculation.
- **`set_value()`** writes, invalidates the cache, and records the change
  in `config_history`. That audit trail is why "the thresholds were tuned
  until the answer looked good" is a checkable claim rather than a matter
  of trust.
- **`fingerprint()`** hashes the whole configuration into a short string
  that is stamped on every model and every pipeline run.
- **`feature_fingerprint()`** hashes *only* the keys listed in
  `features.defining_keys` — the ones that change what a feature value
  means. A change to a cloud price must not invalidate a feature that
  never depended on it.

The single exception is `DB_PATH`. The database location cannot come from
the database, so it is read from the environment
(`RESOURCE_MONITOR_DB`). That exception is deliberate and documented.

Defaults live in `DEFAULT_CONFIG` in
[database/schema.py](database/schema.py) and are seeded with
`INSERT OR IGNORE` — so editing that file changes what a **fresh**
database starts with, and never overwrites a value a running system
already holds.

### One door for SQL

[crud/query.py](crud/query.py) is the only place SQL executes. Every
value is bound as a parameter; nothing interpolates input into a
statement.

| Function | Purpose |
|---|---|
| `execute_query(query, values, fetch)` | Run one statement. Rows when `fetch=True`, else the row count. |
| `execute_insert(query, values)` | Insert and return the **new primary key**. |
| `execute_many(query, rows)` | Bulk insert over one connection, one transaction. |

`execute_insert` exists because `last_insert_rowid()` is scoped to a
connection, and `execute_query` opens a fresh one per call — so issuing
the INSERT and then asking for the id as two separate calls always
returned `0`.

[database/connection.py](database/connection.py) guarantees that by the
time you hold a connection, every table exists and defaults are seeded.
`init_db()` runs once per process. `apply_column_migrations()` handles
columns added after a database was created, since a
`CREATE TABLE IF NOT EXISTS` is a no-op on an existing table — adding a
column is the only migration shape allowed, because SQLite cannot drop or
retype one without rebuilding the table, and silently rebuilding a table
holding measured history is not something an init path should ever do.

---

## 3. How data gets in

### The collector — [collector/psutil_logger.py](collector/psutil_logger.py)

- **`collect_metrics()`** takes one sample. It snapshots
  `psutil.disk_io_counters()` *before* and *after* the blocking
  `cpu_percent(interval=1)` call, so disk read/write MB/s come for free
  from the delta without adding any latency. Returns a dict with `ts`,
  `cpu_percent`, `mem_percent`, `mem_used_mb`, `disk_read_mb_s`,
  `disk_write_mb_s`.
- **`log_once()`** samples and writes one row.
- **`run_logger(interval, duration)`** loops until Ctrl+C or a duration.
  The interval is read at *call* time, not as a default argument — a
  default binds once at import, so the collector would keep using
  whatever the interval was when the module first loaded.

### The load generator — [collector/load_generator.py](collector/load_generator.py)

**`wave_pattern()`** spawns burn workers to drive CPU up and down in a
repeating wave. This is not decoration: an idle laptop produces a flat
line, and a flat line has nothing to forecast. The interesting results in
this project only exist because the machine was made genuinely busy.

### The CRUD console — [crud/console.py](crud/console.py)

The hand-operated surface, shared by `main.py` and `run.bat` so the two
menus cannot drift apart. `view_all`, `view_latest`, `view_between`,
`count`, `create`, `update`, `delete`, `purge`, driven by `run(name)` or
the interactive `menu()`.

Prompts are parsed in Python rather than by the batch file, because
`set /p` would hand an unvalidated string straight to SQL — one mistyped
record id used to raise `ValueError` through the menu and end the
session.

### The storage layer — [crud/metrics_crud.py](crud/metrics_crud.py)

| Function | Purpose |
|---|---|
| `create_metric(record)` | Insert one sample, return its id. |
| `read_all()` / `read_latest(n)` / `read_between(a, b)` | Reads. |
| `count_metrics()` | Row count. |
| `update_metric(id, field, value)` | Update **one whitelisted field**. |
| `delete_metric(id)` | Delete one row. |
| `purge_before(cutoff)` | Bulk delete by age; used by retention. |

`update_metric` checks `field` against a whitelist before it reaches the
SQL string. A column name cannot be bound as a parameter, so the
whitelist *is* the injection defence.

Every write also mirrors to `data/exports/metrics_raw.csv` — inserts
append a line, edits and deletes rewrite the file — so the CSV is never a
stale snapshot someone took by hand. The mirror is imported lazily inside
each writer, so a failure to load the exporter can never stop a database
write.

---

## 4. Stages 1–6: the data plane

All six run inside **`pipeline.etl.run()`**, which owns their ordering and
writes the lineage record. The console output numbers them in *execution*
order, which is why load (stage 3) prints last. One real run, for shape —
the figures move every cycle, because the collector never stops:

```
[1] extract    : 2355 rows from sqlite
[2] conform    : CONFORMS  fingerprint=16411461eade
[4] validate   : WARN (11 pass / 2 warn / 0 fail)
[5] clean      : 2355 -> 2384 rows, 17 segment(s), 29 synthetic
[6] transform  : 31 columns, tiers {'15s': 488, '1min': 130, '5min': 35}
[3] load       : metrics_clean + metrics_rollup (run_id=40)
```

Note that cleaning *added* rows: `regrid` snaps each segment onto an even
axis and `impute` fills short interior gaps, so 2355 raw samples became
2384 cleaned ones with 29 marked `is_imputed`. Nothing invented crosses a
segment boundary.

### Stage 1 — sources — [pipeline/sources.py](pipeline/sources.py)

A `Source` is anything that can `read()` rows and `describe()` where they
came from. Four exist:

| Class | Reads from |
|---|---|
| `SQLiteSource` | the project's own `metrics` table (the default) |
| `StreamSource` | only rows newer than a saved **watermark** |
| `CSVSource` | a CSV file |
| `HTTPSource` | a JSON endpoint |

**`get_source(spec)`** builds one from a URI-style string
(`sqlite://`, `stream://`, `csv://path`, `http://url`), so the source is
a config value rather than a code change.

`StreamSource` is what makes the scheduler viable. A collector appends
forever; reprocessing the whole table every cycle gets slower without
bound. `_load_watermark()` / `_save_watermark()` track the last timestamp
consumed, and `peek(n)` looks without advancing.

### Stage 2 — the contract — [pipeline/schema.py](pipeline/schema.py)

The column rules live in the `schema_contract` **table**, not in code —
types, nullability, min/max, and which columns are forecast targets.

- **`contract(table)`** reads the rules.
- **`coerce(df)`** forces a frame to the declared types where possible.
- **`validate(df)`** checks it and returns a report.
- **`upsert_rule(...)`** is how the contract evolves.

Note that `metrics.id` is declared **nullable** on purpose: the id is
assigned by the database on insert, so a CSV, HTTP or streaming source
legitimately arrives without one. Declaring it `NOT NULL` made every
non-SQLite source fail the gate.

### Stage 4 — validation — [pipeline/validate.py](pipeline/validate.py)

Thirteen checks, each returning `PASS`, `WARN` or `FAIL`. Every threshold
is read from config.

- **`run_gate(df)`** runs them all and returns `(checks, summary)`.
- **`detect_cadence(df)`** measures the real sampling interval *from the
  data*, rather than trusting the configured one.
- **`find_gaps(df)`** finds intervals longer than
  `gap_multiple × cadence` — breaks in collection, not slow samples.
- **`profile(df)`** gives a per-column summary.
- **`persist(run_id, checks)`** writes results to `quality_checks`, so
  data health is queryable over time rather than only visible in a
  console.

The three-verdict split carries real meaning. A constant disk column is a
`WARN` — it is real, it is just not forecastable, and saying so is a
finding. Fifty rows of data is a `FAIL`, because nothing downstream can
produce a defensible number from it. **A `FAIL` stops the run**, which is
the point of a gate.

### Stage 5 — cleaning — [pipeline/clean.py](pipeline/clean.py)

**`clean(df)`** runs these in order:

| Step | What it does |
|---|---|
| `normalise` | coerce types, drop rows with no usable timestamp |
| `deduplicate` | collapse duplicate timestamps by averaging |
| `clip_ranges` | force values into the contract's declared range |
| `segment_on_gaps` | assign `segment_id`, incrementing at every break |
| `regrid` | snap each segment onto an evenly spaced axis |
| `flag_outliers` | Hampel/MAD scoring — marks, never alters |
| `repair_outliers` | acts on flags; the default `flag_only` changes nothing |
| `impute` | time-linear interpolation **inside** segments, bounded run length |

Two of these encode expensive lessons.

**`segment_on_gaps` is why the numbers are honest.** The data has real
collection breaks. Every lag, rolling window and diff downstream runs
inside `groupby("segment_id")`, so no feature ever reaches across one. A
gap-blind build keeps more rows and every one of the extra rows is
contaminated by a window spanning a break.

**`regrid` buckets, it does not match.** An early version used `reindex`
onto a `date_range`, which silently discarded every timestamp that fell
off the grid — about a third of the data, because the series has mixed
cadence (3s / 4s / 5s). `resample` is required. `segment_profile()`
reports each segment's rows, span and *native* cadence.

`repair_outliers` defaulting to `flag_only` is also deliberate: CPU here
moves between 0% and 100% within a few samples, and those transitions are
exactly the events an SLA analysis exists to capture. Deleting them as
"outliers" would delete the phenomenon.

### Stage 6 — transformation — [pipeline/transform.py](pipeline/transform.py)

- **`add_derived(df)`** adds rate-of-change, volatility, a smoothed level
  (EWMA), trend and headroom.
- **`label_regimes(df)`** tags each row `idle` / `ramp` / `saturated`
  using bounds from config. This is the one non-numeric column the system
  produces, and it is later one-hot encoded so the model can see it.
- **`normalise_units(df)`** makes units explicit and consistent.
- **`rollup(df, freq)`** downsamples keeping mean, max and p95;
  **`build_rollups(df)`** produces every retention tier named in config.

Rollups drop empty buckets. `resample` spans the whole range, so a
multi-day collection break once produced tens of thousands of empty
15-second rows.

### Stage 3 — the load and the run record — [pipeline/etl.py](pipeline/etl.py)

| Function | Purpose |
|---|---|
| `run(source_spec, persist, verbose)` | Execute stages 1–6 and load. Returns a result dict. |
| `data_fingerprint(df)` | Short stable hash of the input data. |
| `start_run` / `finish_run` | Open and close the `pipeline_runs` lineage row. |
| `reconcile_stale_runs()` | Close out runs left `running` by a process that died. |
| `load_clean` / `load_rollups` | Write results for this run id. |
| `read_clean(run_id)` | Read a cleaned run back. |
| `latest_run(status)` | The most recent run, `success` by default. |
| `prune_orphaned_loads()` | Delete rows whose run never completed. |

`reconcile_stale_runs()` exists because `finish_run` is only reached by
code still executing. Kill the scheduler window, suspend the laptop,
restart the container, and the row would stay `running` for ever — the
event log claiming a pipeline run is in flight days after its process
died. It reconciles on **age** (`pipeline.stale_run_minutes`), not on
"is there a newer run", because the latter would wrongly flag the
scheduler's loop the first time someone also ran a one-off pipeline pass.

---

## 5. Stages 7–10: features

### Stage 7 — feature engineering — [model/features.py](model/features.py)

- **`build_feature_frame(df, target)`** attaches every feature column and
  the supervised target.
- **`build_features(df, target)`** returns `(X, y, meta)` — the training
  table.
- **`build_serving_row(df, target)`** returns **one** vector for the most
  recent moment, built by the identical construction. That shared
  construction is what stops a feature meaning one thing in training and
  another in serving.
- **`chronological_split(X, y, meta)`** trains on the earlier portion and
  tests on the later. Never shuffled — shuffling a time series leaks the
  future into training.
- **`rolling_origin_splits(X, y, n_folds)`** gives expanding-window CV.
  `RandomizedSearchCV` defaults to shuffled folds, which is why
  [model/tuning.py](model/tuning.py) implements its own loop.

Feature families: lags, rolling statistics, calendar parts, one-hot
encoded regime, and interactions. All of them are built inside
`groupby("segment_id")`.

> **`features.lags` starts at 0.** `lag_0` is the current value — the
> thing the system genuinely knows at prediction time. Without it,
> features end at *t−1* while the target is *t+1*: a two-step problem
> wearing a one-step label. The naive baseline was computed the same way,
> so "next = the value from two steps ago" was the thing being beaten.
> Including `lag_0` makes this a true one-step forecast and makes the
> baseline much harder to beat, because at three-second spacing
> "next = current" is already very strong. That is the honest comparison.

### Stage 8 — selection — [model/selection.py](model/selection.py)

Three screens, applied by **`run(df, target)`**:

1. **`cardinality_screen`** drops constant or near-constant features.
2. **`correlation_screen`** collapses near-duplicates above a threshold.
3. **`importance_screen`** drops features whose shuffling does not
   measurably hurt the model (permutation importance).

**`protected_features(target)`** exempts certain columns from the
correlation screen — notably the target's own `lag_0`, which correlates
heavily with everything and would otherwise be collapsed away.

### Stage 9 — scaling — [model/scaling.py](model/scaling.py)

- **`fit_scaler(train_df, method)`** fits on **training rows only**.
- **`apply_scaler(df, scaler)`** transforms with an already-fitted scaler.
- **`fit_apply(X_train, X_test)`** does both in the only correct order.
- **`compare_methods(split)`** trains GBR and Ridge under every scaling
  and reports the MAE spread; **`invariance_verdicts(table)`** turns that
  spread into a statement — trees are scale-invariant, linear models are
  not, and the table proves it rather than asserting it.

### Stage 10 — the feature store — [model/feature_store.py](model/feature_store.py)

Two stores. The **offline** store (`feature_values`) holds materialised
training rows; the **online** store (`feature_online`) holds the single
latest vector per target, for serving.

| Function | Purpose |
|---|---|
| `materialise(X, y, meta, target, …)` | Write a training set; returns the manifest. |
| `materialise_all(df, …)` | Every target at once. |
| `load_offline(version)` | Read a training set back. |
| `refresh_online(df, …)` | Recompute and publish the current vector per target. |
| `write_online` / `get_online_features` | Publish / read the serving vector. |
| `get_historical_features(entity_df, target)` | Point-in-time join. |

**Two ids, and the difference matters.**

- **`version_id(target, columns, data_fingerprint)`** identifies a
  *materialisation*: this definition against this data snapshot. Right
  for the offline store, where "which rows" is exactly the question.
- **`definition_id(target, columns)`** identifies only what the features
  *mean* — target, column set, and `feature_fingerprint()`. No data.

The train/serve compatibility check compares the **definition**. It used
to compare `version_id`, and because the data fingerprint changes every
time the collector appends a row, a promoted model became unservable
within seconds of promotion and could never recover — the error advised
"re-run the feature store", which produced yet another new fingerprint
and the same refusal. Train/serve skew is a question about definitions,
and definitions do not move when a sample arrives.

`get_historical_features` joins on the **exact** stored timestamp, never
a nearest match. A tolerant join would happily attach a feature computed
after the moment being scored — the classic way a backtest produces a
result that cannot be reproduced live.

---

## 6. Stage 11: training, and the ladder it must climb

### The baseline ladder — [model/baseline.py](model/baseline.py)

Before any model is allowed to claim anything, it is measured against six
alternatives on the **identical** test window:

| Rung | What it predicts |
|---|---|
| `persistence` | next = current value |
| `persistence_lag1` | next = the value *before* current (the old, weaker baseline) |
| `drift` | current + the slope between the last two samples |
| `seasonal_naive` | the value one load-generator period ago |
| `rolling_mean` | the mean of the recent window |
| `ridge` | a linear model on the same features, correctly scaled |

**`run_ladder(split, target, …)`** evaluates all of them and returns a
table; **`best_baseline(table)`** picks the strongest — which is what a
model must actually beat, not the most convenient one.

Baselines are evaluated on the **full** feature frame, because selection
can legitimately drop `lag_1` or `roll_mean` and would otherwise silently
delete rungs from the ladder.

### Training — [model/forecast.py](model/forecast.py)

**`train_one(df, target, …)`** is the centre of the stage. It:

1. builds features and splits chronologically,
2. runs selection and scaling,
3. fits the estimator named by `model.algorithm`,
4. scores it against the whole ladder,
5. runs **rolling-origin CV** and records the mean, the spread, and how
   many folds it won against persistence,
6. re-scores the **incumbent champion on the same window**,
7. materialises the exact training set to the feature store,
8. saves the bundle (model, feature list, `feature_version`,
   `feature_definition`) and calls `register(result)`.

Step 6 was added after a bug: the gate had been comparing a new model's
score against a champion's score recorded on *different, possibly easier*
data. That rejected a model which had genuinely beaten the live baseline.

**`train_all(df, …)`** does this for every target. **`register(result)`**
records the model with its full lineage — feature version, data
fingerprint, config fingerprint, run id, MAE, baseline MAE, CV stats.

`model.flat_threshold` handles the degenerate case: when a signal is
essentially constant, a percentage improvement divides by roughly zero,
so the report gives absolute MAE and says plainly that there is nothing
to forecast.

### Tuning — [model/tuning.py](model/tuning.py)

`sample_grid()` draws candidates, `cv_score()` scores each with
rolling-origin CV, `tune()` searches one target, and `apply_best()`
writes the winner **into the config table** — so a tuned hyperparameter
is a config row like everything else, with a history entry.

---

## 7. Stage 12: the gate, serving, and drift

### The promotion gate — [tracking/mlflow_tracker.py](tracking/mlflow_tracker.py)

**`evaluate(result)`** decides. A challenger is rejected if it:

- loses to the strongest baseline (`promotion.require_beat_baseline`), or
- fails to beat the incumbent by `promotion.min_improvement_pct`, or
- wins fewer than `promotion.min_cv_folds_won` of the CV folds, or
- has a fold-to-fold spread beyond `promotion.max_cv_std_ratio` × the
  baseline's.

**`run_gate(results)`** logs each result, applies `evaluate`, then either
`promote()`s or `record_rejection()`s. If nothing earns the slot,
**`promote_baseline(target, result)`** registers persistence as champion
— so production always serves something whose error has been measured.

> Use `is not None`, not truthiness, when testing MAE. A perfect baseline
> scores `0.0`, which is falsy; treating that as "no baseline" once let a
> worse model through.

The CV-stability criterion exists because of a specific result: a model
beat persistence by ~10% on a single chronological holdout, and
rolling-origin CV showed it won only half the folds and collapsed on the
earliest one — trained on idle data, tested on load. **One favourable
window is not evidence.** A gate that has never rejected anything is
decoration.

`ensure_portable_artifact_root()` handles a real portability bug: MLflow
resolves an experiment's `artifact_location` once, at creation, and
stores it absolute. A Windows-created experiment read inside a container
sent artifacts to `/C:/Users/...` in the container's writable layer,
where they were discarded on exit — and nothing errored, because metrics
and params still reached the database.

### Serving — [serving/predictor.py](serving/predictor.py)

| Function | Purpose |
|---|---|
| `resolve_champion(target)` | The model currently approved to serve. |
| `predict(target, df)` | Next value. Returns `(value, meta)`. |
| `predict_all(df)` | Every target. |
| `forecast_horizon(target, steps)` | Iterative multi-step trajectory. |
| `log_prediction(...)` | Record a prediction for later scoring. |
| `score(df)` | Backfill actuals whose moment has arrived. |
| `backfill(df, target)` | Replay the champion across history. |
| `recent_predictions(target)` | For the accuracy chart. |

`predict` refuses to serve when the champion's `feature_definition` does
not match the online store's, then separately checks that every feature
the model needs is present. Refusing is correct: a model fed a
differently-shaped vector produces a number that looks fine and means
nothing.

`score()` is what makes drift measurable — a prediction is only evidence
once its actual has arrived and the error is recorded.

### Drift — [serving/drift.py](serving/drift.py)

Two independent signals:

- **Performance drift.** `rolling_error(target)` over recent scored
  predictions, against `reference_error(target)` from promotion time.
- **Feature drift.** `psi()` computes Population Stability Index between
  an earlier and a recent window; `feature_drift()` and
  `categorical_drift()` apply it to the inputs and to regime occupancy.

Feature drift matters because performance drift needs actuals, which
arrive a horizon late and never arrive at all if the collector stops — so
without it, a system that has stopped being fed can never notice its
inputs moved.

**`check(target, df)`** decides and on which signal. **`monitor(df)`**
checks every target, writes events via `record_event()`, and retrains
when allowed. **`retrain()`** re-trains and **re-gates** — a drift-
triggered retrain faces exactly the same gate as any other challenger.
`in_cooldown()` prevents retraining on every cycle, since drift persists
until a better model is found.

`drift_events` records both `action` (what the monitor decided) and
`outcome` (what the retrain actually produced). They differ whenever a
retrain ran and the gate then rejected the result — which is the normal
case here, and the thing an event log has to be able to say.

---

## 8. Turning a forecast into money

### The cost model — [service/cost_model.py](service/cost_model.py)

| Function | Purpose |
|---|---|
| `resource_map()` | target → (unit label, node capacity, cost key), from config |
| `units_for_percent(target, pct)` | a utilisation % → provisioned units |
| `monthly_cost(vcpus, ram_gb, storage_gb)` | dollars for an explicit allocation |
| `resource_monthly_cost(target, pct)` | dollars for one resource |
| `cost_for_allocation({target: pct})` | the full breakdown |
| `static_cost()` | the over-provisioned baseline: 100%, always |
| `breach_stats(actual, allocation)` | how badly an allocation under-serves demand |
| `apply_safety_floor(candidate, actual)` | raise until the breach rate complies |
| `sweep(target, actual)` | cost against breach rate across the range |
| `sla_minimum(curve)` | the cheapest compliant point on that curve |

`breach_stats` counts **episodes**, not just a rate. Ten consecutive
breaching samples is one incident, not ten — and an SLA conversation is
about incidents.

### The recommender — [service/recommender.py](service/recommender.py)

**`recommend_percent(target, df)`** is the five-step decision:

1. forecast the horizon using the champion,
2. take the **P95** of that trajectory (the predicted near-peak),
3. add `policy.headroom` → the lean, forecast-driven candidate,
4. apply the **safety floor** — raise until the breach rate on recent
   *actual* demand meets the SLA,
5. price it against static 100% provisioning.

Steps 3 and 4 pull in opposite directions, and **the gap between them is
the finding, not a defect**. The forecast proposes what is efficient; the
floor enforces what is safe. When they disagree sharply, the system is
telling you this signal is not predictable enough to allocate against
aggressively.

**`build_recommendation(df)`** does every resource and totals the cost.
**`tradeoff_curve(target)`** returns the cost-vs-breach curve and its
cheapest compliant point.

`print_report` deliberately prints both the snapshot and a pointer to the
replay, because on this data the two disagree — and the replay is the one
to trust.

### The backtest — [evaluation/backtest.py](evaluation/backtest.py)

This is the honesty mechanism. **`run(df, target)`** walks the series one
decision at a time. At each step the policy sees **only** the rows before
it, decides an allocation, and is then charged for the following
`horizon` samples and judged on whether demand exceeded the allocation
during them. Nothing downstream of the decision point is visible when the
decision is made.

Five policies compete on identical data:

| Policy | What it does |
|---|---|
| `static_100` | always 100% — the over-provisioned default |
| `reactive_p95` | trailing P95 of recent actuals + headroom — **no model at all** |
| `predictive` | the champion's forecast + headroom, then the safety floor |
| `predictive_nofloor` | the same without the floor, to price what the floor costs |
| `oracle` | perfect foresight — unachievable; bounds what forecasting could add |

The gap between `reactive_p95` and `predictive` is the value the model
contributes. The gap between `predictive` and `oracle` is what a perfect
model would add on top. Reporting both keeps the result honest in either
direction.

**`combined(results)`** aggregates to whole-node cost. Its breach rate is
the **worst** across resources, never the pooled average: pooling let
memory's zero breaches dilute CPU's failures, and an earlier version
reported the node as SLA-compliant while CPU alone was breaching well
past budget. An SLA is violated when any one resource violates it.

**The finding this produces is that the saving comes from the allocation
policy, not from forecasting** — `reactive_p95`, which uses no model
whatsoever, is the cheapest SLA-compliant policy. The model-driven policy
saves more only by breaching the SLA. That result is not a bug to be
fixed.

---

## 9. The process alert

[service/process_alert.py](service/process_alert.py) answers the question
an allocation percentage cannot: *what is actually holding the memory?*

- **`live_memory_percent()`** reads `mem_percent` from the **online
  feature store**, not a fresh `psutil` call — so the alert is raised
  against the same number the predictor served, not a moment that existed
  nowhere else in the system.
- **`top_processes()`** returns the heaviest processes by RSS. Processes
  that vanish or deny access mid-scan are skipped; on Windows plenty of
  system processes deny access, and one of them must not take the whole
  alert down.
- **`check()`** fires when live memory crosses
  `policy.capacity_alert_threshold`, respects
  `policy.process_alert_cooldown_sec`, and writes a row to
  `recommendations` with `type='process_alert'` and the process list as a
  JSON payload.

**It suggests; it does not remediate.** There is no `kill()`,
`terminate()`, `send_signal()` or `suspend()` anywhere in it, and that is
a design decision rather than an omission. The system's whole argument is
that a measured recommendation beats a confident one — it rejects its own
models for failing cross-validation. A component that acted
irreversibly on a single threshold crossing would be doing exactly what
the rest of the project refuses to do.

The cooldown exists because memory pressure *persists* — that is what
makes it worth reporting — so without one the scheduler files an
identical top-five list every cycle and buries the table under one
repeated event.

---

## 10. The synthetic twin

Every measured number in this project comes from one laptop over a few
hours. That is honest but narrow: it cannot say whether the policy
finding is a property of the policy or an accident of this host's load.

**[collector/scenario_generator.py](collector/scenario_generator.py)**
builds load shapes the collector never happened to see, writing to the
**same** `metrics` table with the same columns and units:

| Scenario | Shape | What it tests |
|---|---|---|
| `regime_change` | flat idle, then a step to sustained load | the exact CV fold where the model collapsed |
| `sustained_spike` | ramp past 90%, held, released | the capacity alert threshold |
| `gap_injection` | a deliberate collection break | segment-aware lags |
| `cadence_drift` | 3s → 5s → 4s sampling | why cleaning must bucket, not reindex |
| `multi_host_shift` | same shape, busier baseline | the "only one host" limitation |

Every parameter is a `twin.*` config row, including the seed — so the
same settings regenerate the same series, and retuning a scenario until
it says something flattering leaves a dated trail in `config_history`.

**[orchestration/run_twin.py](orchestration/run_twin.py)** replays the
**unmodified** pipeline against a scenario and scores it exactly as
production is scored. `run_twin()` runs the stages; `format_policy_table()`
prints the same columns as the README's table; `format_comparison()`
answers whether the ranking holds or flips across all five.

`NOT_DEPLOYABLE` excludes `static_100` (it is the thing being measured
against, so it would be circular) and `oracle` (it sees the future, so it
will always be cheapest — letting it win made the twin announce a flip
that had not happened).

**[orchestration/twin_paths.py](orchestration/twin_paths.py)** is the
wall. A twin run executes the *real* trainer, so everything that makes it
convincing also makes it dangerous — the code does not know it is a
rehearsal, and would drop `.joblib` files next to the ones serving
production.

- **`assert_not_production(db_path)`** refuses unless the path is
  unmistakably a twin: the filename must contain `twin`, it must not
  resolve to a known production path, and `os.path.samefile` catches a
  symlink or bind mount pointing at the real thing. It **raises** rather
  than using `assert`, because `python -O` strips assert statements and a
  guard that vanishes under an optimisation flag is not a guard.
- **`isolate_artifacts(db_path)`** redirects the model directory, MLflow
  store and stream watermark under `data/twin_artifacts/<scenario>/`
  **before the first `import config`** — the ordering is load-bearing,
  because `config.DB_PATH` and friends resolve once, at module load.

The current result: `reactive_p95` still wins in three of five scenarios,
`predictive` wins under `multi_host_shift`, and under `sustained_spike`
**no** deployable policy meets the SLA. Both outcomes are reportable; the
scenarios are not tuned toward either.

---

## 11. Running it: one pass, or forever

### One pass — [orchestration/run_pipeline.py](orchestration/run_pipeline.py)

`python -m orchestration.run_pipeline` runs every stage in order and
prints a stage-by-stage report: config → ETL (1–6) → features → selection
→ scaling → feature store → [tuning] → training → gate → inference and
drift → backtest → recommendation. A blocked quality gate stops the run
and returns `1`.

### Forever — [orchestration/scheduler.py](orchestration/scheduler.py)

`python -m orchestration.scheduler --with-collector` is the
"continuously analyses" clause. Each **`cycle()`**:

1. incremental ETL from the stream watermark,
2. refresh the online feature store,
3. predict with the champion, logged for later scoring,
4. score — backfill actuals whose moment has passed,
   **4b. process alert** — positioned *after* predict because it reads
   the refreshed `mem_percent`, and *before* drift and recommend because
   it must not influence either,
5. drift check every `--drift-every` cycles,
6. recommend and price, written to the database,
7. retention.

**`start_collector()`** runs the psutil logger on a daemon thread, so one
window both gathers data and acts on it.

**`acquire_lock()` / `release_lock()`** enforce a single instance. Two
schedulers would sample the same machine twice into the same table at a
3-second cadence, which reads downstream as duplicate timestamps and a
cadence that never happened. A stale lock left by a killed process is
detected (the PID must still exist *and* be a Python process, since PIDs
are recycled) and ignored.

**The scheduler never promotes a model.** Drift can trigger a retrain,
and the retrain faces the same gate as any other challenger.

### The launcher — [run.bat](run.bat)

`run.bat` with no arguments starts the live system *before* showing the
menu:

```
  dashboard : starting  -> http://localhost:8501
  scheduler : starting with the collector in-process
```

Each gets its own window and keeps running after the menu exits. That is
what makes the dashboard dynamic rather than a still photograph of the
last pipeline run: the collector appends every few seconds, the scheduler
re-forecasts and re-prices each cycle, and the dashboard re-reads every
ten.

The dashboard is deliberately **not** a menu entry — Streamlit holds its
console for as long as it serves, so launching it from the menu ended the
session that launched it.

---

## 12. The dashboard

`streamlit run dashboard/app.py`, or just `run.bat`. Nine tabs; a
customer account sees the first four, an admin sees all of them, decided
by a `role` lookup against a table the user cannot write — not by which
half of the login screen was submitted.

| Tab | Shows |
|---|---|
| **Overview** | champions, cost position, allocations, live utilisation, process alerts |
| **Forecast** | the horizon with a 95% band, and a demand-spike stress test |
| **Capacity** | allocation cylinders, forecast, threshold, recommended addition |
| **Simulation** | run a synthetic scenario and compare every policy |
| Data Health | the quality gate, collection gaps, segments, cleaning audit |
| Model | the baseline ladder, registry, promotion decisions, drift |
| Cost & SLA | the walk-forward backtest and the tradeoff curve |
| Lineage & Config | trace a champion to its data — and **edit config live** |
| Logs | six tables flattened into one chronological event stream |

Everything on screen is a stored artifact of a pipeline run, so no two
panels can disagree. The one exception is the config editor, which
writes — and that is deliberate: changing `policy.headroom` there and
watching the cost move is the clearest demonstration that nothing in this
system is hardcoded.

**The Capacity tab** draws each resource as a vertical cylinder filled to
current usage, with the forecast peak and the alert threshold marked.
Every cylinder has **identical geometry** — same width, same height — so
only the fill varies, on one shared 0–100% scale. An earlier version
sized each cylinder against its own capacity, which made a full CPU
cylinder and a full disk cylinder look identical while meaning nothing
alike. When the forecast crosses the threshold, a dashed **recommended
addition** cylinder appears; its size is unit arithmetic on the
recommender's own allocation, not a second allocation decision invented
in the dashboard.

**The Simulation tab** launches the twin as a **detached** background
process writing to a log file, and polls it. The earlier version called
`subprocess.run()` and waited — but the sidebar auto-refresh reruns the
script every ten seconds, and Streamlit abandons a running script when a
rerun arrives, so the work was being torn down mid-flight and its output
discarded. Detaching inverts that: each auto-refresh becomes a poll
rather than an interruption, and the log is what gets shown on failure.

**The Logs tab** is worth noting. The system writes no log *file*. Every
event it has ever produced is a row in one of six tables — pipeline runs,
quality checks, model versions, drift events, config history, process
alerts — flattened into one stream by a single SQL union. A log line can
disagree with the database; a row cannot.

[dashboard/theme.py](dashboard/theme.py) holds the entire visual system,
so `app.py` contains no colours, no font sizes and no emoji.

---

## 13. Lineage: closing the loop

[tracking/lineage.py](tracking/lineage.py) is what lets any number on the
dashboard be defended.

- **`trace_model(model_id)`** returns everything upstream of one model:
  its feature version and the exact column list, the data fingerprint,
  the ETL run that produced that data, the quality warnings that applied
  at the time, and the config fingerprint.
- **`trace_prediction(...)`** goes from a logged prediction back to the
  model and its inputs.
- **`config_at(fingerprint)`** reconstructs which config values a
  fingerprint corresponded to.

So the full chain, in both directions:

```
prediction -> model -> feature version -> ETL run -> raw samples
                    -> config fingerprint -> the exact settings
                    -> quality checks that were WARNing at the time
```

That is the claim the project actually rests on: not that the forecast is
good — on this data it is not good enough to deploy — but that every
number it produces can be traced to its origin, and that the mechanisms
which measure it were built well enough to catch the project's own
mistakes. They did so three times: a "+15.2% over naive" that turned out
to be measured two steps back; a 10% holdout win that cross-validation
showed was luck; and a gate comparing scores taken on different data.

---

## 14. Function reference

Quick index of the main entry points, by module.

### Configuration and storage

| Module | Function | Does |
|---|---|---|
| `config` | `get` / `get_int` / `get_float` / `get_bool` / `get_json` | typed reads from the `config` table |
| | `set_value` | write + invalidate + history entry |
| | `fingerprint` / `feature_fingerprint` | config hashes for lineage / feature versioning |
| `crud.query` | `execute_query` / `execute_insert` / `execute_many` | the only place SQL runs |
| `crud.metrics_crud` | `create_metric` / `read_*` / `update_metric` / `delete_metric` / `purge_before` | CRUD on `metrics`, mirrored to CSV |
| `crud.config_crud` | `get_raw` / `set_value` / `history` / `serialise` / `deserialise` | storage layer for config |
| `database.connection` | `get_connection` / `init_db` / `apply_column_migrations` | connect, create, seed, migrate |

### Collection

| Module | Function | Does |
|---|---|---|
| `collector.psutil_logger` | `collect_metrics` / `log_once` / `run_logger` | sample this machine |
| `collector.load_generator` | `wave_pattern` | make the CPU signal forecastable |
| `collector.scenario_generator` | `generate` / `generate_*` / `resolve_settings` / `bind_database` | synthetic scenarios into a twin DB |

### Data plane (stages 1–6)

| Module | Function | Does |
|---|---|---|
| `pipeline.sources` | `get_source` / `SQLiteSource` / `StreamSource` / `CSVSource` / `HTTPSource` | where rows come from |
| `pipeline.schema` | `contract` / `coerce` / `validate` / `upsert_rule` | the column contract |
| `pipeline.validate` | `run_gate` / `detect_cadence` / `find_gaps` / `profile` / `persist` | 13 checks → PASS/WARN/FAIL |
| `pipeline.clean` | `clean` / `segment_on_gaps` / `regrid` / `flag_outliers` / `impute` / `segment_profile` | dedupe, segment, regrid, impute |
| `pipeline.transform` | `transform` / `add_derived` / `label_regimes` / `build_rollups` | derived columns, regimes, tiers |
| `pipeline.etl` | `run` / `data_fingerprint` / `load_clean` / `read_clean` / `latest_run` / `reconcile_stale_runs` | sequences 1–6, writes lineage |

### Features (stages 7–10)

| Module | Function | Does |
|---|---|---|
| `model.features` | `build_features` / `build_serving_row` / `chronological_split` / `rolling_origin_splits` | gap-aware features, honest splits |
| `model.selection` | `run` / `cardinality_screen` / `correlation_screen` / `importance_screen` | three screens |
| `model.scaling` | `fit_scaler` / `fit_apply` / `compare_methods` / `invariance_verdicts` | fit on train only |
| `model.feature_store` | `materialise` / `refresh_online` / `get_online_features` / `version_id` / `definition_id` | offline + online, versioned |

### Model and gate (stages 11–12)

| Module | Function | Does |
|---|---|---|
| `model.baseline` | `run_ladder` / `best_baseline` / `persistence` / `drift` / `seasonal_naive` / `ridge` | what the model must beat |
| `model.forecast` | `train_one` / `train_all` / `register` / `load_model` / `predict_next` | train, evaluate, register |
| `model.tuning` | `tune` / `cv_score` / `apply_best` | rolling-origin hyperparameter search |
| `model.horizon` | `predict_horizon` / `horizon_summary` | multi-step wrapper |
| `tracking.mlflow_tracker` | `evaluate` / `run_gate` / `promote` / `promote_baseline` / `registry` | the promotion gate |
| `tracking.lineage` | `trace_model` / `trace_prediction` / `config_at` / `champions` | where did this come from |

### Serving and evidence

| Module | Function | Does |
|---|---|---|
| `serving.predictor` | `predict` / `predict_all` / `forecast_horizon` / `score` / `backfill` / `resolve_champion` | serve the champion, log, score |
| `serving.drift` | `monitor` / `check` / `psi` / `feature_drift` / `rolling_error` / `retrain` | detect drift, retrain, re-gate |
| `service.cost_model` | `monthly_cost` / `static_cost` / `breach_stats` / `apply_safety_floor` / `sweep` | percentages → dollars |
| `service.recommender` | `recommend_percent` / `build_recommendation` / `tradeoff_curve` | forecast → allocation → price |
| `service.process_alert` | `check` / `top_processes` / `live_memory_percent` / `recent` | suggest, never act |
| `evaluation.backtest` | `run` / `run_all` / `combined` / `policy_*` | replay every decision |

### Orchestration and UI

| Module | Function | Does |
|---|---|---|
| `orchestration.run_pipeline` | `main` | all stages, one command |
| `orchestration.scheduler` | `run` / `cycle` / `start_collector` / `acquire_lock` | the continuous loop |
| `orchestration.run_twin` | `run_twin` / `run_all_scenarios` / `format_comparison` | replay against synthetic load |
| `orchestration.twin_paths` | `assert_not_production` / `isolate_artifacts` | the wall around production |
| `conversion.csv_export` | `mirror_insert` / `mirror_rebuild` / `export_*` | keep CSVs in step |
| `dashboard.app` | (Streamlit script) | nine tabs over stored artifacts |
| `dashboard.theme` | `page` / `section` / `bullet` / `cylinders` / `status_tag` | the whole visual system |
| `dashboard.auth` | `require_login` / `logout` | Supabase auth, role from a table |

---

## Appendix: the rules that must not be broken

Collected from the design decisions above, because each one was a bug
first.

1. **Nothing is hardcoded.** New settings go in `DEFAULT_CONFIG`.
2. **Segment boundaries are hard walls.** Every lag, window and diff runs
   inside `groupby("segment_id")`.
3. **`features.lags` starts at 0.** Otherwise it is a two-step problem
   wearing a one-step label.
4. **Chronological split, never shuffle.** Rolling-origin CV for folds.
5. **Scalers fit on training rows only.**
6. **Baselines evaluate on the FULL feature frame**, or selection
   silently deletes ladder rungs.
7. **Parameterised SQL only.** Dynamic column names get a whitelist.
8. **`execute_insert` for inserts needing the new id.**
9. **Use `is not None`, not truthiness, on MAE.** A perfect baseline
   scores `0.0`.
10. **Regrid buckets, it does not match.** `resample`, never `reindex`.
11. **The train/serve check compares the feature *definition*, not the
    materialisation.** Data changes constantly; definitions do not.
12. **No twin code path touches production** — not the database, not the
    model directory, not MLflow.
13. **`dataset/` is tracked evidence; `data/` is regenerable output.**

# What every file does — in plain English

One entry per file. Each says what the file is for and why it matters,
without assuming you already know the system. Where a term could be
unclear, it is explained in the same line.

The data moves in one direction: something measures the machine → the
numbers are cleaned → turned into inputs a model can use → a model
forecasts the next value → that forecast becomes a resource-allocation
decision → the decision is priced and replayed to check the saving is
real. The files below are grouped by their place in that flow.

---

## Top level

| File | What it does | Why it matters |
|---|---|---|
| **run.bat** | The one-click launcher for Windows. Checks that Python, the virtual environment, and the libraries are all present, creates or installs anything missing, initialises the database, then runs whatever you asked for (menu, pipeline, dashboard, collector, drift check…). | It is the "prerequisites" front door. A new machine can go from a fresh clone to a running system with a single command, and it refuses to run further if a prerequisite is genuinely missing rather than failing halfway with a confusing error. |
| **config.py** | The one place the rest of the code asks "what is the value of setting X?". It reads settings out of the database and hands them back as the right type (number, list, true/false). | Nothing in the project hardcodes a price, a threshold, or a model setting. They all live in the database and are read through here, so the system can be re-tuned without editing code. |
| **main.py** | A text menu that can reach every part of the system — collect data, run the pipeline, train, look at results, edit settings. | The human entry point for people who prefer a menu over commands. |
| **requirements.txt** | The exact list of Python libraries and their versions. | Pins the versions the system was built and measured against, so it behaves the same on another machine. |
| **README.md** | Project overview and how to run it. | The starting point for anyone new to the repository. |
| **CLAUDE.md** | Project rules, architecture, current results, and the reasoning behind key decisions. | The "why it is built this way" document; it records the mistakes that were caught and the rules that prevent them recurring. |
| **FILES.md** | This document. | A plain-English map of the whole codebase. |
| **MLOPSProject.code-workspace** | A VS Code workspace file. | Convenience for opening the project in the editor; not part of the running system. |

---

## `collector/` — measuring the machine

| File | What it does | Why it matters |
|---|---|---|
| **collector/psutil_logger.py** | Every few seconds, reads the machine's real CPU, memory, and disk usage and saves one row to the database. | This is where all the data comes from. Without it there is nothing to forecast. |
| **collector/load_generator.py** | Deliberately makes the CPU busy in a repeating wave pattern (idle → busy → idle). | An idle laptop produces a flat line, and you cannot forecast a flat line. This creates a real, rising-and-falling signal so the forecasting has something meaningful to predict — and switching its pattern is what makes the drift alarm fire during a demo. |

---

## `conversion/` — data in and out as files

| File | What it does | Why it matters |
|---|---|---|
| **conversion/csv_export.py** | Saves the raw data, a cleaned run, or a set of model inputs out to CSV files. | Lets you hand the data to someone else, or feed a file back into the pipeline to replay exactly what happened. |

---

## `crud/` — talking to the database safely

CRUD = **C**reate, **R**ead, **U**pdate, **D**elete: the four basic things
you do with stored records.

| File | What it does | Why it matters |
|---|---|---|
| **crud/query.py** | The single doorway through which all database commands pass. | Every value is passed safely as data, never glued into the command text, which closes off the most common database security hole. Having one doorway means that guarantee is made in exactly one place. |
| **crud/metrics_crud.py** | Create, read, update, and delete rows of measured metrics. | This is the literal "CRUD program" the project brief asks for. Its update function only allows editing an approved list of columns, so a caller cannot trick it into touching something it should not. |
| **crud/config_crud.py** | Read and write the settings table, and keep a history of every change. | Every setting change is recorded with its old and new value, so you can always see what was changed and when. |

---

## `database/` — the shape of the storage

| File | What it does | Why it matters |
|---|---|---|
| **database/schema.py** | Defines every table in the database and every default setting, in one place. Also lists the small "add this column" upgrades applied to databases created before those columns existed. | This is the single source of truth for the database's shape. Defaults are seeded only once and then owned by the database, so editing a default here changes what a brand-new database starts with, not one that is already running. |
| **database/connection.py** | Opens the database and, on first use, makes sure every table exists, the default settings are present, and any pending column upgrades are applied. | Guarantees that by the time any code touches the database, it is fully set up. The upgrade step means an existing database with real history gets new columns added without being wiped and rebuilt. |

---

## `pipeline/` — turning raw numbers into clean, usable data

This is the "continuously analyse historical usage" part of the brief.
The stages run in order.

| File | What it does | Why it matters |
|---|---|---|
| **pipeline/sources.py** | Defines where data can be read from: the live database, a CSV file, a web address, or an incremental "only what's new" feed. | The rest of the pipeline does not care where the data came from, so switching source is a setting, not a rewrite. |
| **pipeline/schema.py** | Forces incoming data into the expected types and reports anything that still does not fit the agreed rules. | Raw feeds are messy. This makes the data predictable before anything trusts it, and the rules themselves live in the database so they can be tightened without code changes. |
| **pipeline/etl.py** | The conductor: runs extract → check → clean → transform → save, in order, and writes a record of every run (how many rows came in, how many survived, whether the quality check passed). | If the data fails the quality check, this stops the run rather than training on bad data. Every run leaves a paper trail, so any later result can be traced back to the exact data that produced it. |
| **pipeline/validate.py** | Runs a set of quality checks (missing values, duplicates, sensible ranges) and grades the data PASS, WARN, or FAIL. | The gatekeeper. A FAIL stops the pipeline; a WARN is noted but allowed. This is what stops "garbage in" from silently becoming "garbage out". |
| **pipeline/clean.py** | Removes duplicate timestamps, forces values into sensible ranges, spots gaps where collection stopped, puts the surviving data onto an even time grid, and flags outliers. | Turns ragged real-world readings into a tidy, evenly-spaced series. Crucially, it marks the gaps so nothing later treats "before the gap" and "after the gap" as if they were seconds apart. |
| **pipeline/transform.py** | Adds helpful derived columns (how fast a value is changing, how bumpy it is, how much room is left before it maxes out), labels each row as **idle / ramp / saturated**, calls the encoder, and produces summarised versions at coarser time steps. | This is the "analysis" stage. It creates the richer descriptions of each moment that a model can actually learn from, rather than just the bare readings. |
| **pipeline/encode.py** | **(new)** Turns the text label `regime` (idle / ramp / saturated) into numbers a model can read — either one yes/no column per category, or a single ranked number. | A model cannot read the word "saturated". Before this file, that label was thrown away and the model never learned which part of its range it was in. The important detail: the list of possible categories is fixed in settings, not read from the data in front of it — so a single live row produces the exact same columns as the full training set. Reading the categories from the data instead is a classic bug where a live prediction quietly gets the wrong columns and returns a plausible-looking but wrong answer. |

---

## `model/` — building inputs and forecasting

This is the "forecast CPU / memory / storage" part of the brief.

| File | What it does | Why it matters |
|---|---|---|
| **model/features.py** | Builds the actual table the model learns from: recent past values, rolling summaries, the encoded regime, and the answer it is trying to predict (the next value). Builds it identically for training and for live prediction. | The heart of getting the model right. It respects collection gaps (never using "history" from across a break) and includes the current value as an input, which makes the prediction an honest one-step-ahead forecast. Training and live prediction share this one code path, so a feature cannot mean one thing in training and another in production. |
| **model/baseline.py** | Defines six simple, no-machine-learning ways to guess the next value (e.g. "it'll be the same as now"). | This is the bar the model has to clear. Comparing the model against the *strongest* simple guess — not a deliberately weak one — is what makes "the model is good" an honest claim instead of a flattering one. |
| **model/selection.py** | Removes input columns that carry no information, that duplicate another column, or that the model demonstrably ignores. | Fewer, more useful inputs make a model faster and less likely to latch onto noise. |
| **model/scaling.py** | Puts inputs onto comparable numeric scales, learning those scales from the training data only. | If the scaling "peeked" at the test data, the test score would be secretly inflated and unreliable. Learning it from training data alone keeps the evaluation honest. |
| **model/feature_store.py** | Saves the exact set of model inputs used, stamped with a version, and keeps the single latest input row ready for live prediction. | Lets you answer, months later, "exactly what did this model train on?" and guarantees live prediction uses the same input definition the model was trained with. |
| **model/forecast.py** | Trains a forecasting model for each resource, scores it against all six simple baselines and across several time windows, and records the result with its full history. | The main modelling file. It does not just report one score; it checks whether a win holds up across different periods, because a single lucky window is not proof. |
| **model/tuning.py** | Searches for better model settings, always testing them on future data the model has not seen. | Finds good settings without the common mistake of accidentally testing on data from the future, which would give an impressive score that never holds up in reality. |
| **model/horizon.py** | Produces a multi-step forecast (the next minute, not just the next reading) by reusing the live-prediction code. | Reuses one prediction path instead of keeping a second copy, so the two cannot drift apart over time. |

---

## `tracking/` — recording and gatekeeping

This is the operations ("MLOps") backbone.

| File | What it does | Why it matters |
|---|---|---|
| **tracking/mlflow_tracker.py** | Logs every training run, and runs the **promotion gate** — the rules that decide whether a freshly trained model is actually allowed to be used. | The gate has real teeth: a model must beat the strongest simple baseline, win across multiple time windows, and be stable, or it is rejected and a simple, measured fallback is used instead. This is what stops a worse-than-trivial model from reaching production just because someone trained it. |
| **tracking/lineage.py** | Given any prediction, walks the chain backwards: which model made it → which inputs → which data run → which settings → what the quality checks said. | Full traceability. Any number the system produces can be explained end to end, which is what makes the results auditable rather than "trust me". |

---

## `serving/` — using the model live

This is the "deployment" part of the brief.

| File | What it does | Why it matters |
|---|---|---|
| **serving/predictor.py** | Serves whatever the gate approved (a trained model, or the simple fallback), records every prediction, and later fills in what actually happened so the prediction can be scored. | One interface for making and logging predictions. Recording predictions and later comparing them to reality is what makes it possible to notice the model getting worse — otherwise "it's degrading" is just a feeling. |
| **serving/drift.py** | **(completed)** Watches for three kinds of trouble and retrains when needed: (1) predictions getting less accurate, (2) the input numbers shifting, (3) the mix of idle/ramp/saturated changing. When it retrains, the new model still has to pass the same promotion gate, and the result is written back onto the alert. | This is the "keeps working over time" part. It reuses the encoder to spot when the machine's behaviour changes (e.g. the load pattern switches), avoids retraining over and over on the same problem (a cooldown), and records honestly whether each retrain actually produced a better model or was rejected. |

---

## `service/` — turning forecasts into money decisions

This is the "recommend allocation and reduce cost" part of the brief.

| File | What it does | Why it matters |
|---|---|---|
| **service/cost_model.py** | Converts a usage percentage into provisioned units and a monthly dollar figure, and counts not just how often demand exceeded the allocation but whether those breaches were isolated blips or one sustained outage. | Dollars are the point of the whole project. Counting sustained breaches separately matters because "5% of readings were over" can mean fifty harmless one-second spikes or one long failure — very different for a reliability promise. |
| **service/recommender.py** | For each resource: forecast the near-future peak, add a safety buffer, then raise the allocation until recent real demand would have stayed within the reliability target — and price the result against always paying for 100%. | This is the actual recommendation. The gap between the lean forecast-driven number and the safe number is itself a finding: it shows the real tension between saving money and keeping the reliability promise. |

---

## `evaluation/` — proving the saving is real

| File | What it does | Why it matters |
|---|---|---|
| **evaluation/backtest.py** | Replays the whole history one decision at a time, letting each policy see only the past, then charges it for what happened next and checks whether it kept within the reliability target. | This is what turns a *projected* saving into a *measured* one. Most projects stop at "the model is accurate"; this one replays every decision to check the money and reliability claims actually hold up. |

---

## `orchestration/` — running everything together

| File | What it does | Why it matters |
|---|---|---|
| **orchestration/run_pipeline.py** | Runs every stage from configuration through data, model, gate, serving, recommendation, and backtest — in order, in one command. | The "do the whole thing once" button. Proves all the pieces fit together and produces a full result in a single run. |
| **orchestration/scheduler.py** | The continuous loop: repeatedly pull new data, predict, score old predictions, check for drift, and write fresh recommendations. Can also run the collector in the same process. | This is what "continuously monitors" actually means in practice — the system running on its own, cycle after cycle, rather than a one-off script. |

---

## `dashboard/` — seeing it all

| File | What it does | Why it matters |
|---|---|---|
| **dashboard/app.py** | A web dashboard (five tabs) that shows the data, forecasts, model results, and costs — and lets you change settings live. | The visual front end. Because settings live in the database, editing them here changes the running system without touching code. |

---

## Generated folders (not source code)

| Folder | What it holds |
|---|---|
| **data/** | The live database, the MLflow tracking database, saved model files, and CSV exports. This is the system's memory. |
| **mlruns/** | MLflow's own record of training runs. |
| **graphify-out/** | An auto-generated knowledge-graph and wiki view of the codebase, for navigation. |
| **.venv/** | The private Python environment with all the installed libraries. Created and managed by `run.bat`. |

---

## The four brief clauses, and where each one lives

1. **Continuously analyse historical usage** → `collector/`, `pipeline/`, `orchestration/scheduler.py`
2. **Forecast CPU / memory / storage** → `model/`
3. **Recommend allocation strategies** → `service/recommender.py`
4. **Improve performance and reduce cost** → `service/cost_model.py`, `evaluation/backtest.py`

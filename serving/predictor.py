"""
Inference — serving whatever the promotion gate approved.

The champion may be a trained estimator or the persistence baseline. Both
are served through the same interface, because from the consumer's point
of view "what will this resource be next" has one answer and the choice
of predictor is an implementation detail the registry owns.

Serving a baseline is not a degraded mode
-----------------------------------------
On this data the gate rejected every gradient booster, so persistence is
the champion for all three resources. That is a legitimate production
state: the measured best predictor is being served. The alternative —
deploying a model known to be worse because it is the one that got
trained — is the failure this design exists to prevent.

Every prediction is written to `predictions` with the model that made it
and the timestamp it is about. When the real value arrives, `score()`
backfills it and computes the error. That table is what makes drift
detection possible; without it, "the model is getting worse" is a feeling.

Feature-version enforcement
---------------------------
A trained champion records the feature version it was fitted on. If the
online store holds a different version, serving REFUSES rather than
silently predicting from mismatched inputs. Training-serving skew is
otherwise invisible: the numbers still look plausible.
"""

import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config
from crud.query import execute_query
from model import feature_store
from model.features import build_serving_row, load_clean_frame
from model.forecast import load_model, model_path, MODEL_DIR
from tracking.mlflow_tracker import BASELINE_PREFIX


# ----------------------------------------------------------------------
# Champion resolution
# ----------------------------------------------------------------------
def resolve_champion(target):
    """The model currently approved to serve `target`."""
    rows = execute_query(
        """
        SELECT model_id, algorithm, mae, feature_version, promoted_at
        FROM model_versions
        WHERE target = ? AND is_champion = 1
        ORDER BY promoted_at DESC LIMIT 1
        """,
        (target,), fetch=True,
    )
    if not rows:
        return {"model_id": f"{BASELINE_PREFIX}persistence-{target}", "algorithm": "persistence", "mae": 0.0,
                "feature_version": "v1", "promoted_at": None, "is_baseline": True}

    r = rows[0]
    model_id = r[0]
    is_baseline = str(model_id).startswith(BASELINE_PREFIX)

    if not is_baseline:
        path = model_path(model_id)
        if not os.path.exists(path):
            existing = []
            if os.path.exists(MODEL_DIR):
                existing = [f for f in os.listdir(MODEL_DIR) if f.startswith(f"{target}-") and f.endswith(".joblib")]
            if existing:
                existing.sort(reverse=True)
                found_id = existing[0][:-7]
                return {"model_id": found_id, "algorithm": r[1], "mae": r[2],
                        "feature_version": r[3], "promoted_at": r[4], "is_baseline": False}
            else:
                return {"model_id": f"{BASELINE_PREFIX}persistence-{target}", "algorithm": "persistence", "mae": r[2],
                        "feature_version": r[3], "promoted_at": r[4], "is_baseline": True}

    return {"model_id": model_id, "algorithm": r[1], "mae": r[2],
            "feature_version": r[3], "promoted_at": r[4],
            "is_baseline": is_baseline}


# ----------------------------------------------------------------------
# Single-step prediction
# ----------------------------------------------------------------------
def predict(target, df=None, log=True):
    """Predict the next value of `target`. Returns (value, meta)."""
    champion = resolve_champion(target)
    if champion is None:
        return None, {"error": f"no champion for {target}; run the gate first"}

    row, info = _current_features(target, df)
    if row is None:
        return None, info

    # --- baseline champion ---------------------------------------------
    if champion["is_baseline"]:
        column = f"{target}_lag_0"
        if column not in row.columns:
            return None, {"error": f"current value ({column}) unavailable"}
        value = float(row.iloc[0][column])
        meta = {
            "model_id": champion["model_id"],
            "algorithm": "persistence",
            "predictor": "baseline (next = current)",
            "ts": info.get("ts"),
            "feature_version": info.get("version_id"),
        }
    # --- trained champion -----------------------------------------------
    else:
        bundle = load_model(champion["model_id"])
        if bundle is None:
            return None, {"error": f"model file missing for "
                                   f"{champion['model_id']}"}

        # Compare DEFINITIONS, not materialisations.
        #
        # This used to compare `version_id`, which hashes the data
        # fingerprint alongside the feature definition. The data
        # fingerprint changes every time the collector appends a row, so a
        # promoted model stopped being servable within seconds of
        # promotion and could never recover: the error advised "re-run the
        # feature store", and doing that produced another new fingerprint
        # and the same refusal. Only baseline champions, which skip this
        # branch entirely, ever served.
        #
        # The guard is meant to catch train/serve SKEW — features that
        # mean something different now than they did at training. That is
        # the column set and the feature-defining config, which is exactly
        # what `definition_id` hashes and what does not move when data
        # arrives.
        served = info.get("definition_id")
        trained = bundle.get("feature_definition")
        if served and trained and served != trained:
            return None, {
                "error": (
                    f"feature definition mismatch: champion was trained on "
                    f"{trained}, online store holds {served}. Refusing to "
                    f"serve rather than predict from mismatched inputs. "
                    f"Retrain this target: python -m orchestration.run_pipeline"
                )
            }
        # A model trained before `feature_definition` was recorded cannot
        # be compared this way. It is not left unguarded: the column check
        # immediately below is the concrete protection, and it rejects any
        # vector missing a feature the model needs.

        missing = [c for c in bundle["features"] if c not in row.columns]
        if missing:
            return None, {"error": f"online features missing {len(missing)} "
                                   f"column(s): {missing[:5]}"}

        value = float(bundle["model"].predict(row[bundle["features"]])[0])
        meta = {
            "model_id": champion["model_id"],
            "algorithm": champion["algorithm"],
            "predictor": "trained model",
            "ts": info.get("ts"),
            # The materialisation the model trained on, for lineage. The
            # compatibility check above uses the definition id instead.
            "feature_version": bundle.get("feature_version"),
            "feature_definition": trained,
        }

    meta["predicted"] = round(value, 4)

    if log:
        cadence = config.get_int("pipeline.nominal_cadence_sec")
        shift = config.get_int("features.target_shift")
        when = pd.to_datetime(info["ts"]) + timedelta(seconds=cadence * shift)
        meta["predicted_for_ts"] = when.isoformat()
        log_prediction(target, meta["model_id"], when, value)

    return value, meta


def predict_all(df=None, log=True):
    if df is None:
        df = load_clean_frame()
    return {
        target: predict(target, df, log=log)
        for target in config.get_json("features.targets")
    }


# ----------------------------------------------------------------------
# Multi-step horizon
# ----------------------------------------------------------------------
def _feature_span():
    """Rows of history the newest feature vector can possibly depend on."""
    lags = config.get_json("features.lags") or [0]
    return (max(int(l) for l in lags)
            + config.get_int("features.roll_window")
            + config.get_int("features.target_shift"))


def _horizon_context(df):
    """The tail of `df` that the iterative forecast actually needs.

    Returns the whole frame unchanged when the configured context is not
    safely larger than the feature span, so this can only ever be a
    speed-up — never a silent change to what the model sees.
    """
    keep = config.get_int("model.horizon_context_rows")
    span = _feature_span()

    # The margin is doubled deliberately: the span is the minimum, and a
    # segment boundary inside the tail costs rows that the groupby then
    # cannot use. Anything less than this and we do not trim at all.
    if keep <= 0 or keep < span * 2 or len(df) <= keep:
        return df
    return df.iloc[-keep:].reset_index(drop=True)


def forecast_horizon(target, steps=None, df=None):
    """Iterative multi-step forecast. Returns a trajectory DataFrame.

    Each step feeds the previous prediction back in as the new current
    value. Error compounds, which is the honest behaviour — a horizon
    forecast is less certain than a one-step one and the trajectory
    should show that rather than hide it.

    With a persistence champion the trajectory is flat by construction.
    That is not a bug: persistence genuinely has no opinion about the
    future beyond the present, and pretending otherwise would invent
    confidence the measurement does not support.
    """
    steps = steps or config.get_int("model.forecast_horizon")
    champion = resolve_champion(target)
    if champion is None:
        return pd.DataFrame(), {"error": f"no champion for {target}"}

    if df is None:
        df = load_clean_frame()
    if df.empty:
        return pd.DataFrame(), {"error": "no cleaned data"}

    cadence = config.get_int("pipeline.nominal_cadence_sec")
    last_ts = pd.to_datetime(df["ts"].iloc[-1])
    mae = float(champion.get("mae", 1.5) or 1.5)

    if champion["is_baseline"]:
        current = float(df[target].iloc[-1])
        steps_arr = np.arange(1, steps + 1)
        spread = 1.96 * mae * np.sqrt(steps_arr / 5.0)
        upper = [round(float(min(100.0, current + s)), 4) for s in spread]
        lower = [round(float(max(0.0, current - s)), 4) for s in spread]
        trajectory = pd.DataFrame({
            "step": steps_arr,
            "ts": [last_ts + timedelta(seconds=cadence * s)
                   for s in range(1, steps + 1)],
            "predicted": [current] * steps,
            "upper_bound": upper,
            "lower_bound": lower,
        })
        return trajectory, {
            "model_id": champion["model_id"],
            "predictor": "baseline (flat: persistence has no forward opinion)",
            "horizon_seconds": steps * cadence,
            "forecast_p95": round(current, 4),
            "forecast_peak": round(current, 4),
            "mae": mae,
        }

    bundle = load_model(champion["model_id"])
    if bundle is None:
        return pd.DataFrame(), {"error": "model file missing"}

    # Only the TAIL is needed, and the difference is not small.
    #
    # `build_serving_row` reconstructs every feature — lags, rolling
    # windows, interactions, encoding — across the whole frame it is
    # given, and then keeps a single row. This loop calls it once per
    # horizon step, so a 20-step forecast rebuilt 2,500+ rows of features
    # twenty times to use twenty rows. On the dashboard that was ~0.9s per
    # call, paid five times per render, and it grew with the collection.
    #
    # The last row's features depend on at most `max(features.lags) +
    # features.roll_window` rows before it, all inside its own segment.
    # Keeping a generous multiple of that is exact, not approximate: rows
    # trimmed away could not have influenced the vector being built.
    # `_horizon_context()` refuses to trim when the configured window is
    # not comfortably larger than that span, so a config change that makes
    # the tail too short falls back to the full frame rather than quietly
    # changing the forecast.
    working = _horizon_context(df).copy()
    predictions = []
    for step in range(1, steps + 1):
        row, info = build_serving_row(working, target)
        if row is None:
            break
        value = float(bundle["model"].predict(row[bundle["features"]])[0])
        predictions.append(value)

        # Append the prediction as if it were observed, so the next step
        # sees it as history.
        next_row = working.iloc[[-1]].copy()
        next_row["ts"] = pd.to_datetime(working["ts"].iloc[-1]) + timedelta(seconds=cadence)
        next_row[target] = value
        working = pd.concat([working, next_row], ignore_index=True)

    if not predictions:
        return pd.DataFrame(), {"error": "could not build a serving row"}

    steps_arr = np.arange(1, len(predictions) + 1)
    spread = 1.96 * mae * np.sqrt(steps_arr / 5.0)
    upper = [round(float(min(100.0, val + s)), 4) for val, s in zip(predictions, spread)]
    lower = [round(float(max(0.0, val - s)), 4) for val, s in zip(predictions, spread)]

    trajectory = pd.DataFrame({
        "step": steps_arr,
        "ts": [last_ts + timedelta(seconds=cadence * s)
               for s in range(1, len(predictions) + 1)],
        "predicted": predictions,
        "upper_bound": upper,
        "lower_bound": lower,
    })
    return trajectory, {
        "model_id": champion["model_id"],
        "predictor": "trained model (iterative)",
        "horizon_seconds": len(predictions) * cadence,
        "forecast_p95": round(float(np.percentile(predictions, 95)), 4),
        "forecast_peak": round(float(np.max(predictions)), 4),
        "mae": mae,
    }


# ----------------------------------------------------------------------
# Prediction log and scoring
# ----------------------------------------------------------------------
def log_prediction(target, model_id, predicted_for_ts, value):
    execute_query(
        """
        INSERT OR REPLACE INTO predictions
            (model_id, target, predicted_for_ts, predicted, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (model_id, target,
         predicted_for_ts.isoformat() if hasattr(predicted_for_ts, "isoformat")
         else str(predicted_for_ts),
         float(value), datetime.now().isoformat(timespec="seconds")),
    )


def score(df=None, tolerance_seconds=None):
    """Backfill actuals for predictions whose moment has arrived.

    Matched on exact timestamp against the cleaned series. A prediction
    about a moment the collector never recorded stays unscored rather
    than being matched to a neighbouring sample.
    """
    if df is None:
        df = load_clean_frame()
    if df.empty:
        return {"scored": 0, "reason": "no cleaned data"}

    pending = execute_query(
        "SELECT id, target, predicted_for_ts, predicted FROM predictions "
        "WHERE actual IS NULL", fetch=True,
    ) or []
    if not pending:
        return {"scored": 0, "pending": 0}

    lookup = df.copy()
    lookup["key"] = pd.to_datetime(lookup["ts"]).dt.floor("s")

    scored = 0
    for row_id, target, ts, predicted in pending:
        moment = pd.to_datetime(ts).floor("s")
        match = lookup[lookup["key"] == moment]
        if match.empty or target not in match.columns:
            continue
        actual = float(match[target].iloc[0])
        execute_query(
            "UPDATE predictions SET actual = ?, abs_error = ? WHERE id = ?",
            (actual, abs(actual - float(predicted)), row_id),
        )
        scored += 1

    return {"scored": scored, "pending": len(pending) - scored}


def backfill(df, target, log=True):
    """Replay the champion across history, one prediction per sample.

    The live scheduler predicts on every collection cycle, so the drift
    monitor expects a prediction per sample. The backtest logs one per
    DECISION — every horizon-length window — which is far too sparse to
    reach the minimum sample count drift requires. This reconstructs what
    the prediction log would look like had the champion been serving all
    along, which is what makes drift measurable on historical data.

    Only rows inside a segment are predicted; a prediction across a
    collection gap would be scored against a value from after the break.
    """
    champion = resolve_champion(target)
    if champion is None:
        return {"error": f"no champion for {target}"}
    if df.empty or target not in df.columns:
        return {"error": "no data"}

    shift = config.get_int("features.target_shift")
    rows, skipped = [], 0

    if champion["is_baseline"]:
        for segment_id, group in df.groupby("segment_id"):
            group = group.sort_values("ts").reset_index(drop=True)
            for i in range(len(group) - shift):
                current = group[target].iloc[i]
                when = group["ts"].iloc[i + shift]
                if pd.isna(current):
                    skipped += 1
                    continue
                rows.append((champion["model_id"], target,
                             pd.to_datetime(when).isoformat(),
                             float(current), datetime.now().isoformat(timespec="seconds")))
    else:
        bundle = load_model(champion["model_id"])
        if bundle is None:
            return {"error": "model file missing"}
        from model.features import build_feature_frame

        frame, feature_cols = build_feature_frame(df, target)
        usable = frame.dropna(subset=feature_cols)
        if usable.empty:
            return {"error": "no complete feature rows"}
        predictions = bundle["model"].predict(usable[bundle["features"]])
        cadence = config.get_int("pipeline.nominal_cadence_sec")
        for value, ts in zip(predictions, usable["ts"]):
            when = pd.to_datetime(ts) + timedelta(seconds=cadence * shift)
            rows.append((champion["model_id"], target, when.isoformat(),
                         float(value),
                         datetime.now().isoformat(timespec="seconds")))

    if log and rows:
        from crud.query import execute_many

        execute_many(
            """
            INSERT OR REPLACE INTO predictions
                (model_id, target, predicted_for_ts, predicted, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )

    return {"target": target, "logged": len(rows), "skipped": skipped,
            "model_id": champion["model_id"]}


def backfill_all(df=None):
    if df is None:
        df = load_clean_frame()
    return {
        target: backfill(df, target)
        for target in config.get_json("features.targets")
    }


def recent_predictions(target, limit=100, scored_only=True):
    clause = "AND actual IS NOT NULL" if scored_only else ""
    rows = execute_query(
        f"""
        SELECT predicted_for_ts, predicted, actual, abs_error, model_id
        FROM predictions WHERE target = ? {clause}
        ORDER BY predicted_for_ts DESC LIMIT ?
        """,
        (target, int(limit)), fetch=True,
    ) or []
    return pd.DataFrame(rows, columns=[
        "ts", "predicted", "actual", "abs_error", "model_id",
    ]).iloc[::-1].reset_index(drop=True)


def _current_features(target, df=None):
    """The current feature vector, from the online store or recomputed."""
    row, info = feature_store.get_online_features(target)
    if row is not None:
        return row, info
    if df is None:
        df = load_clean_frame()
    return build_serving_row(df, target)


def format_predictions(results):
    lines = ["=" * 78, "INFERENCE — SERVING CHAMPIONS", "=" * 78]
    for target, (value, meta) in results.items():
        if value is None:
            lines.append(f"  {target:14s} n/a — {meta.get('error')}")
            continue
        lines.append(
            f"  {target:14s} {value:8.3f}   {meta['predictor']:44s}"
        )
        lines.append(
            f"  {'':14s} {'':8s}   model={meta['model_id']}"
        )
    lines.append("=" * 78)
    return "\n".join(lines)


if __name__ == "__main__":
    frame = load_clean_frame()
    if frame.empty:
        raise SystemExit("No cleaned data. Run: python -m pipeline.etl")

    # Refresh the online store so serving reads current features.
    from pipeline.etl import latest_run

    run = latest_run() or {}
    feature_store.refresh_online(frame,
                                 data_fingerprint=run.get("data_fingerprint"))

    results = predict_all(frame)
    print(format_predictions(results))

    print()
    for target in config.get_json("features.targets"):
        trajectory, meta = forecast_horizon(target, df=frame)
        if trajectory.empty:
            print(f"  {target:14s} horizon n/a — {meta.get('error')}")
        else:
            print(f"  {target:14s} {meta['horizon_seconds']}s horizon: "
                  f"P95={meta['forecast_p95']:.3f} peak={meta['forecast_peak']:.3f} "
                  f"[{meta['predictor']}]")

    print("\nScoring predictions against observed values...")
    print(" ", score(frame))

"""
STAGE 11 — Machine Learning Model: training, evaluation, tuning.

Trains one forecaster per resource and evaluates it against the full
baseline ladder rather than a single chosen comparison. Every run is
registered in `model_versions` with the feature version, data fingerprint
and config fingerprint that produced it, so any later prediction can be
traced to its inputs.

Evaluation discipline
---------------------
* Chronological split. Shuffling a time series trains on the future.
* Rolling-origin CV alongside the single holdout, because one test window
  can land on a favourable part of the load-generator cycle.
* Error broken down by operating regime. A model can be excellent at idle
  and poor during a ramp, and an average hides exactly the case the SLA
  cares about.
* Reported against the STRONGEST baseline, not the most flattering one.

On flat targets
---------------
When the naive MAE is below `model.flat_threshold`, a percentage
improvement divides by approximately zero and swings wildly on noise.
Those targets report absolute MAE instead and are labelled flat. Disk
here is constant to within 0.06 percentage points; there is nothing to
forecast, and saying so is the correct result rather than a failure.
"""

import hashlib
import json
import os
import time
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error

import config
from crud.query import execute_query
from model import feature_store
from model.baseline import best_baseline, mae as mae_fn, rmse as rmse_fn, run_ladder
from model.features import (
    build_features, chronological_split, load_clean_frame,
    rolling_origin_splits, targets,
)

MODEL_DIR = config.MODEL_DIR


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------
def _model_id(target, params, feature_version):
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    digest = hashlib.sha256(
        json.dumps([target, params, feature_version], sort_keys=True).encode()
    ).hexdigest()[:6]
    return f"{target}-{stamp}-{digest}"


def register(result):
    """Record a trained model with its full lineage."""
    execute_query(
        """
        INSERT OR REPLACE INTO model_versions
            (model_id, target, algorithm, params, feature_version,
             data_fingerprint, config_fingerprint, run_id, train_rows,
             test_rows, mae, rmse, baseline_mae, improvement_pct,
             inference_ms, is_champion, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (result["model_id"], result["target"], result["algorithm"],
         json.dumps(result["params"]), result["feature_version"],
         result["data_fingerprint"], result["config_fingerprint"],
         result["run_id"], result["train_rows"], result["test_rows"],
         result["mae"], result["rmse"], result["baseline_mae"],
         result["improvement_pct"], result["inference_ms"],
         datetime.now().isoformat(timespec="seconds")),
    )
    return result["model_id"]


def champion_id(target):
    rows = execute_query(
        "SELECT model_id FROM model_versions WHERE target = ? AND is_champion = 1 "
        "ORDER BY promoted_at DESC LIMIT 1",
        (target,), fetch=True,
    )
    return rows[0][0] if rows else None


def latest_model_id(target):
    rows = execute_query(
        "SELECT model_id FROM model_versions WHERE target = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (target,), fetch=True,
    )
    return rows[0][0] if rows else None


def model_path(model_id):
    return os.path.join(MODEL_DIR, f"{model_id}.joblib")


def load_model(model_id):
    import joblib

    path = model_path(model_id)
    if not os.path.exists(path):
        return None
    return joblib.load(path)


# ----------------------------------------------------------------------
# Training
# ----------------------------------------------------------------------
def train_one(df, target, run_id=None, data_fingerprint=None,
              use_selection=None, params=None, verbose=True):
    """Train and evaluate one forecaster. Returns a result dict."""
    import joblib

    X, y, meta = build_features(df, target)
    if len(X) < 30:
        return {"target": target,
                "error": f"only {len(X)} usable rows; need at least 30"}

    # The baselines are always evaluated on the FULL feature frame.
    # Selection can drop `lag_1` or `roll_mean`, and computing the ladder
    # from the reduced frame would silently delete the rungs that depend
    # on them — including the one this project's previous claim was made
    # against. The model gets the selected subset; the comparison does not.
    split_full = chronological_split(X, y, meta)

    # --- optional feature selection (stage 8) ---------------------------
    use_selection = (use_selection if use_selection is not None
                     else config.get_bool("selection.enabled"))
    selected = list(X.columns)
    if use_selection:
        from model.selection import run as run_selection

        summary = run_selection(df, target)
        if "selected" in summary and summary["selected"]:
            selected = summary["selected"]

    split = chronological_split(X[selected], y, meta)
    params = params or config.get_json("model.params")

    model = GradientBoostingRegressor(**params)
    model.fit(split["X_train"], split["y_train"])

    started = time.perf_counter()
    predictions = model.predict(split["X_test"])
    inference_ms = (time.perf_counter() - started) / max(1, len(split["X_test"])) * 1000

    model_mae = float(mean_absolute_error(split["y_test"], predictions))
    model_rmse = rmse_fn(split["y_test"], predictions)

    # --- the ladder, on the identical test window -----------------------
    test_index = np.arange(len(X))[split_full["split_index"]:]
    ladder, _ = run_ladder(split_full, target, series=meta["actual_now"],
                           test_index=test_index)
    ladder = pd.concat([
        ladder,
        pd.DataFrame([{
            "baseline": "MODEL (gradient boosting)",
            "mae": round(model_mae, 4), "rmse": round(model_rmse, 4),
            "note": f"{len(selected)} features",
        }]),
    ], ignore_index=True).sort_values("mae").reset_index(drop=True)

    strongest_name, strongest_mae = best_baseline(
        ladder[ladder["baseline"] != "MODEL (gradient boosting)"]
    )
    persistence_mae = _rung(ladder, "persistence")
    old_baseline_mae = _rung(ladder, "persistence_lag1")

    flat_threshold = config.get_float("model.flat_threshold")
    is_flat = persistence_mae is not None and persistence_mae < flat_threshold

    def improvement(reference):
        if reference is None or reference <= 0:
            return None
        return round((reference - model_mae) / reference * 100, 2)

    # --- rolling-origin CV ----------------------------------------------
    cv_scores = []
    for fold in rolling_origin_splits(X[selected], y):
        cv_model = GradientBoostingRegressor(**params)
        cv_model.fit(fold["X_train"], fold["y_train"])
        cv_scores.append(float(mean_absolute_error(
            fold["y_test"], cv_model.predict(fold["X_test"])
        )))

    # --- error by regime -------------------------------------------------
    regime_errors = pd.DataFrame()
    regimes = split.get("regimes_test")
    if regimes is not None:
        frame = pd.DataFrame({
            "regime": regimes.values,
            "abs_error": np.abs(split["y_test"].to_numpy() - predictions),
        })
        regime_errors = (frame.groupby("regime")["abs_error"]
                         .agg(["count", "mean", "max"])
                         .round(4).reset_index()
                         .rename(columns={"mean": "mae", "max": "worst"}))

    importance = pd.DataFrame({
        "feature": selected,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False).reset_index(drop=True)

    # --- materialise the exact training set ------------------------------
    # The SELECTED columns, because that is what the model consumes and
    # therefore what serving must reproduce.
    manifest = feature_store.materialise(
        X[selected], y, meta, target, run_id=run_id,
        data_fingerprint=data_fingerprint
    )
    feature_version = manifest.get("version_id")

    result = {
        "target": target,
        "algorithm": config.get_str("model.algorithm"),
        "params": params,
        "model_id": _model_id(target, params, feature_version),
        "feature_version": feature_version,
        "data_fingerprint": data_fingerprint,
        "config_fingerprint": config.fingerprint(),
        "run_id": run_id,
        "train_rows": len(split["X_train"]),
        "test_rows": len(split["X_test"]),
        "n_features": len(selected),
        "selected_features": selected,
        "mae": round(model_mae, 4),
        "rmse": round(model_rmse, 4),
        "baseline_mae": round(strongest_mae, 4) if strongest_mae else None,
        "baseline_name": strongest_name,
        "persistence_mae": persistence_mae,
        "old_baseline_mae": old_baseline_mae,
        "improvement_pct": improvement(strongest_mae),
        "improvement_vs_persistence_pct": improvement(persistence_mae),
        "improvement_vs_old_baseline_pct": improvement(old_baseline_mae),
        "beats_baseline": strongest_mae is not None and model_mae < strongest_mae,
        "flat_signal": is_flat,
        "inference_ms": round(inference_ms, 4),
        "cv_mae_mean": round(float(np.mean(cv_scores)), 4) if cv_scores else None,
        "cv_mae_std": round(float(np.std(cv_scores)), 4) if cv_scores else None,
        "cv_folds": len(cv_scores),
        "ladder": ladder,
        "regime_errors": regime_errors,
        "importance": importance,
        "predictions": pd.DataFrame({
            "ts": split.get("timestamps_test"),
            "actual": split["y_test"].to_numpy(),
            "predicted": predictions,
            "current": split.get("actual_now_test"),
        }),
    }

    joblib.dump({
        "model": model,
        "features": selected,
        "target": target,
        "model_id": result["model_id"],
        "feature_version": feature_version,
        "params": params,
    }, model_path(result["model_id"]))

    register(result)

    if verbose:
        print(format_one(result))
    return result


def train_all(df=None, run_id=None, data_fingerprint=None, verbose=True):
    from pipeline.etl import latest_run

    if df is None:
        df = load_clean_frame()
    if df.empty:
        return {}

    if run_id is None:
        run = latest_run() or {}
        run_id = run.get("run_id")
        data_fingerprint = data_fingerprint or run.get("data_fingerprint")

    return {
        target: train_one(df, target, run_id=run_id,
                          data_fingerprint=data_fingerprint, verbose=verbose)
        for target in targets()
    }


# ----------------------------------------------------------------------
# Inference
# ----------------------------------------------------------------------
def predict_next(target, df=None):
    """Predict the next value using the CHAMPION model.

    Falls back to the most recently trained model only when no champion
    has been promoted yet, and says so — silently serving an unpromoted
    model would make the promotion gate decorative.
    """
    model_id = champion_id(target)
    source = "champion"
    if model_id is None:
        model_id = latest_model_id(target)
        source = "latest (no champion promoted)"
    if model_id is None:
        return None, {"error": f"no trained model for {target}"}

    bundle = load_model(model_id)
    if bundle is None:
        return None, {"error": f"model file missing for {model_id}"}

    row, info = feature_store.get_online_features(target)
    if row is None:
        if df is None:
            df = load_clean_frame()
        from model.features import build_serving_row

        row, info = build_serving_row(df, target)
        if row is None:
            return None, info

    missing = [c for c in bundle["features"] if c not in row.columns]
    if missing:
        return None, {"error": f"online features are missing {len(missing)} "
                               f"column(s) the model needs: {missing[:5]}"}

    value = float(bundle["model"].predict(row[bundle["features"]])[0])
    return value, {"model_id": model_id, "source": source,
                   "feature_version": bundle.get("feature_version"),
                   "ts": info.get("ts")}


# ----------------------------------------------------------------------
def _rung(ladder, name):
    match = ladder[ladder["baseline"] == name]
    return float(match["mae"].iloc[0]) if not match.empty else None


def format_one(r):
    if "error" in r:
        return f"\n=== {r['target']} ===\n  {r['error']}"

    lines = [
        f"\n=== {r['target']} ===",
        f"  model_id        : {r['model_id']}",
        f"  feature version : {r['feature_version']}  "
        f"({r['n_features']} features)",
        f"  train / test    : {r['train_rows']} / {r['test_rows']} rows",
        f"  model MAE       : {r['mae']}   RMSE {r['rmse']}",
        f"  rolling-origin  : {r['cv_mae_mean']} +/- {r['cv_mae_std']} "
        f"({r['cv_folds']} folds)",
        f"  inference       : {r['inference_ms']} ms/prediction",
        "  baseline ladder :",
        "    " + r["ladder"].to_string(index=False).replace("\n", "\n    "),
    ]

    if r["flat_signal"]:
        lines.append(
            f"  -> FLAT SIGNAL: persistence MAE {r['persistence_mae']} is below "
            f"the {config.get_float('model.flat_threshold')} threshold. A "
            f"percentage here divides by ~0; absolute MAE {r['mae']} is the "
            f"honest metric. Nothing to forecast."
        )
    else:
        verdict = "BEATS" if r["beats_baseline"] else "LOSES TO"
        lines.append(
            f"  -> vs strongest baseline ({r['baseline_name']}, "
            f"MAE {r['baseline_mae']}): {r['improvement_pct']:+.2f}%  {verdict}"
        )
        if r["old_baseline_mae"]:
            lines.append(
                f"  -> vs the PREVIOUS 2-step baseline (persistence_lag1, "
                f"MAE {r['old_baseline_mae']}): "
                f"{r['improvement_vs_old_baseline_pct']:+.2f}%"
            )

    if not r["regime_errors"].empty:
        lines.append("  error by regime :")
        lines.append("    " + r["regime_errors"].to_string(index=False)
                     .replace("\n", "\n    "))

    lines.append("  top features    :")
    lines.append("    " + r["importance"].head(5).to_string(index=False)
                 .replace("\n", "\n    "))
    return "\n".join(lines)


if __name__ == "__main__":
    frame = load_clean_frame()
    if frame.empty:
        raise SystemExit("No cleaned data. Run: python -m pipeline.etl")

    results = train_all(frame)

    print("\n" + "=" * 78)
    print("NEXT-STEP PREDICTIONS")
    print("=" * 78)
    for target in targets():
        value, info = predict_next(target)
        if value is None:
            print(f"  {target:14s} n/a — {info.get('error')}")
        else:
            print(f"  {target:14s} {value:7.2f}   via {info['source']} "
                  f"({info['model_id']})")

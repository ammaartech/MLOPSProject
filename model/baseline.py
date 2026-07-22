"""
Baseline ladder — what the model has to beat.

A single "naive" number is easy to choose badly. This evaluates six
predictors on the identical test window, so "the model is good" becomes a
comparison against the best simple alternative rather than the most
convenient one.

    persistence          next = current value            <- the honest naive
    persistence_lag1     next = value one step BEFORE current
    drift                current + recent slope
    seasonal_naive       the value one load-generator period ago
    rolling_mean         the mean of the recent window
    ridge                a linear model on the same features

Why `persistence_lag1` is on the ladder
---------------------------------------
It is not a serious predictor. It is what the previous version of this
project measured as its baseline, because the smallest lag feature was
`lag_1` and the target was `shift(-1)` — so the "naive" prediction came
from two steps before the value being predicted. Keeping both rungs makes
the difference measurable instead of arguable: the gap between them is
the entire reported improvement.

At a three-second cadence, consecutive samples correlate at roughly 0.97.
Persistence is therefore a strong predictor, and beating it is a real
result rather than a formality.
"""

import numpy as np
import pandas as pd

import config


def mae(y_true, y_pred):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mask = np.isfinite(y_true) & np.isfinite(y_pred)
    if not mask.any():
        return float("nan")
    return float(np.mean(np.abs(y_true[mask] - y_pred[mask])))


def rmse(y_true, y_pred):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mask = np.isfinite(y_true) & np.isfinite(y_pred)
    if not mask.any():
        return float("nan")
    return float(np.sqrt(np.mean((y_true[mask] - y_pred[mask]) ** 2)))


# ----------------------------------------------------------------------
# Individual rungs
# ----------------------------------------------------------------------
def persistence(X_test, target):
    """next = current. The honest one-step naive."""
    column = f"{target}_lag_0"
    return X_test[column].to_numpy(dtype=float) if column in X_test else None


def persistence_lag1(X_test, target):
    """next = the value one step before current. The previous baseline."""
    column = f"{target}_lag_1"
    return X_test[column].to_numpy(dtype=float) if column in X_test else None


def drift(X_test, target):
    """current + the slope between the last two samples."""
    now, before = f"{target}_lag_0", f"{target}_lag_1"
    if now not in X_test or before not in X_test:
        return None
    current = X_test[now].to_numpy(dtype=float)
    return current + (current - X_test[before].to_numpy(dtype=float))


def seasonal_naive(X_test, target, series=None, test_index=None):
    """The value one load-generator period ago.

    The generator runs a 240-second sine and the cadence is 3s, so the
    period is 80 samples — `model.seasonal_lag`. Needs the raw series
    because the lag is deeper than any engineered feature.
    """
    if series is None or test_index is None:
        return None
    lag = config.get_int("model.seasonal_lag")
    values = np.asarray(series, dtype=float)
    return np.array([
        values[i - lag] if i - lag >= 0 else np.nan for i in test_index
    ])


def rolling_mean(X_test, target):
    column = f"{target}_roll_mean"
    return X_test[column].to_numpy(dtype=float) if column in X_test else None


def ridge(split, alpha=None):
    """A linear model on the same features, correctly scaled."""
    from sklearn.linear_model import Ridge

    from model.scaling import fit_apply

    alpha = alpha if alpha is not None else config.get_float("model.ridge_alpha")
    train_scaled, test_scaled, _ = fit_apply(
        split["X_train"], split["X_test"], method="minmax"
    )
    model = Ridge(alpha=alpha).fit(train_scaled, split["y_train"])
    return model.predict(test_scaled)


# ----------------------------------------------------------------------
# The ladder
# ----------------------------------------------------------------------
def run_ladder(split, target, series=None, test_index=None):
    """Evaluate every baseline on the same test window. Returns a table."""
    y_test = split["y_test"].to_numpy(dtype=float)
    X_test = split["X_test"]

    predictions = {
        "persistence": persistence(X_test, target),
        "persistence_lag1": persistence_lag1(X_test, target),
        "drift": drift(X_test, target),
        "seasonal_naive": seasonal_naive(X_test, target, series, test_index),
        "rolling_mean": rolling_mean(X_test, target),
        "ridge": ridge(split),
    }

    notes = {
        "persistence": "next = current value",
        "persistence_lag1": "next = value BEFORE current (the previous baseline)",
        "drift": "current + recent slope",
        "seasonal_naive": f"value {config.get_int('model.seasonal_lag')} samples ago",
        "rolling_mean": "mean of the recent window",
        "ridge": "linear model, min-max scaled",
    }

    rows = []
    for name, prediction in predictions.items():
        if prediction is None or not np.isfinite(np.asarray(prediction, float)).any():
            continue
        rows.append({
            "baseline": name,
            "mae": round(mae(y_test, prediction), 4),
            "rmse": round(rmse(y_test, prediction), 4),
            "note": notes[name],
        })

    table = pd.DataFrame(rows).sort_values("mae").reset_index(drop=True)
    return table, predictions


def best_baseline(table):
    """The strongest rung — what a model must actually beat."""
    if table.empty:
        return None, float("nan")
    row = table.iloc[0]
    return row["baseline"], float(row["mae"])


def naive_forecast(series):
    """Backwards-compatible helper: (y_true, y_pred) for persistence."""
    values = np.asarray(series, dtype=float)
    return values[1:], values[:-1]


if __name__ == "__main__":
    from model.features import (
        build_features, chronological_split, load_clean_frame, targets,
    )

    frame = load_clean_frame()
    if frame.empty:
        raise SystemExit("No cleaned data. Run: python -m pipeline.etl")

    for target in targets():
        X, y, meta = build_features(frame, target)
        if X.empty:
            continue
        split = chronological_split(X, y, meta)
        index = np.arange(len(X))[split["split_index"]:]
        table, _ = run_ladder(split, target, series=meta["actual_now"],
                              test_index=index)

        print(f"\n=== {target} ===  (test rows: {len(split['y_test'])})")
        print(table.to_string(index=False))
        name, value = best_baseline(table)
        print(f"  -> strongest baseline: {name} at MAE {value}")

"""
Multi-step (horizon) forecasting.

The single-step model predicts only the next value. To recommend
allocation we need to see the near-future PEAK, not just the next
instant. So we forecast iteratively: predict step t+1, append it to
the series, rebuild features, predict t+2, and so on for N steps.

This is what makes the recommendation genuinely forecast-driven:
we size for the predicted peak over the horizon, not past history.
"""

import os
import joblib
import numpy as np
import pandas as pd

from config import DATA_DIR, FORECAST_HORIZON
from model.features import load_dataframe, build_features, LAGS, ROLL_WINDOW

MODEL_DIR = os.path.join(DATA_DIR, "models")


def _load_model(target):
    path = os.path.join(MODEL_DIR, f"{target}.joblib")
    if not os.path.exists(path):
        return None, None
    bundle = joblib.load(path)
    return bundle["model"], bundle["features"]


def _build_feature_row(history, target, feature_cols, ts):
    """
    Build ONE feature row (matching training features) from the current
    history list, for a synthetic future timestamp `ts`.
    """
    s = pd.Series(history)

    row = {}
    for lag in LAGS:
        # value `lag` steps back from the end of history
        row[f"{target}_lag_{lag}"] = s.iloc[-lag] if len(s) >= lag else s.iloc[0]

    window = s.tail(ROLL_WINDOW)
    row[f"{target}_roll_mean"] = window.mean()
    row[f"{target}_roll_std"] = window.std() if len(window) > 1 else 0.0
    row[f"{target}_roll_max"] = window.max()

    row["hour"] = ts.hour
    row["minute"] = ts.minute
    row["dayofweek"] = ts.dayofweek

    # order columns exactly as training
    return pd.DataFrame([[row[c] for c in feature_cols]], columns=feature_cols)


def predict_horizon(target, steps=FORECAST_HORIZON):
    """
    Forecast the next `steps` values of `target`, iteratively.
    Returns a list of predicted values (length = steps), or None.
    """
    model, feature_cols = _load_model(target)
    if model is None:
        print(f"No trained model for {target}. Run train_all() first.")
        return None

    df = load_dataframe()
    if df.empty:
        return None

    df = df.sort_values("ts").reset_index(drop=True)
    history = df[target].tolist()
    last_ts = df["ts"].iloc[-1]

    # infer sampling interval from the last two timestamps (fallback 3s)
    if len(df) >= 2:
        interval = df["ts"].iloc[-1] - df["ts"].iloc[-2]
    else:
        interval = pd.Timedelta(seconds=3)

    preds = []
    for step in range(1, steps + 1):
        future_ts = last_ts + interval * step
        X_row = _build_feature_row(history, target, feature_cols, future_ts)
        yhat = float(model.predict(X_row)[0])
        # clamp to valid percentage range
        yhat = max(0.0, min(100.0, yhat))
        preds.append(yhat)
        history.append(yhat)  # feed prediction forward

    return preds


def horizon_summary(target, steps=FORECAST_HORIZON):
    """Convenience: return peak/P95/mean of the forecasted horizon."""
    preds = predict_horizon(target, steps)
    if preds is None:
        return None
    arr = np.array(preds)
    return {
        "target": target,
        "steps": steps,
        "forecast_peak": round(float(arr.max()), 2),
        "forecast_p95": round(float(np.percentile(arr, 95)), 2),
        "forecast_mean": round(float(arr.mean()), 2),
        "all_predictions": [round(p, 2) for p in preds],
    }


if __name__ == "__main__":
    from model.features import TARGETS
    print(f"Horizon forecast ({FORECAST_HORIZON} steps ahead):\n")
    for tgt in TARGETS:
        summary = horizon_summary(tgt)
        if summary:
            print(f"{tgt}")
            print(f"  peak over horizon : {summary['forecast_peak']}%")
            print(f"  P95 over horizon  : {summary['forecast_p95']}%")
            print(f"  mean over horizon : {summary['forecast_mean']}%")
            print(f"  trajectory        : {summary['all_predictions']}\n")
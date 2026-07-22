"""
Multi-step forecasting — now a thin wrapper over the serving path.

This module used to build feature rows itself, with its own copy of the
lag and rolling logic. That is the classic route to training-serving
skew: two implementations of "what does lag_5 mean", drifting apart
whenever one is edited. It also predated gap segmentation, so its windows
reached across breaks in collection.

The real implementation is `serving.predictor.forecast_horizon`, which
rolls the CHAMPION forward using `model.features.build_serving_row` — the
same construction training uses. These functions remain so existing
callers keep working.
"""

import numpy as np

import config


def predict_horizon(target, steps=None, df=None):
    """The forecast trajectory as a plain list of values."""
    from serving.predictor import forecast_horizon

    trajectory, meta = forecast_horizon(target, steps=steps, df=df)
    if trajectory.empty:
        print(f"No forecast for {target}: {meta.get('error')}")
        return None
    return [round(float(v), 2) for v in trajectory["predicted"]]


def horizon_summary(target, steps=None, df=None):
    """Peak, P95 and mean of the forecast horizon."""
    from serving.predictor import forecast_horizon

    trajectory, meta = forecast_horizon(target, steps=steps, df=df)
    if trajectory.empty:
        return None

    values = trajectory["predicted"].to_numpy(dtype=float)
    return {
        "target": target,
        "steps": len(values),
        "predictor": meta.get("predictor"),
        "model_id": meta.get("model_id"),
        "forecast_peak": round(float(values.max()), 2),
        "forecast_p95": round(float(np.percentile(values, 95)), 2),
        "forecast_mean": round(float(values.mean()), 2),
        "all_predictions": [round(float(v), 2) for v in values],
    }


if __name__ == "__main__":
    from model.features import load_clean_frame

    frame = load_clean_frame()
    if frame.empty:
        raise SystemExit("No cleaned data. Run: python -m pipeline.etl")

    steps = config.get_int("model.forecast_horizon")
    print(f"Horizon forecast ({steps} steps = "
          f"{steps * config.get_int('pipeline.nominal_cadence_sec')}s ahead)\n")

    for target in config.get_json("features.targets"):
        summary = horizon_summary(target, df=frame)
        if not summary:
            print(f"{target}: unavailable\n")
            continue
        print(f"{target}   [{summary['predictor']}]")
        print(f"  peak over horizon : {summary['forecast_peak']}%")
        print(f"  P95 over horizon  : {summary['forecast_p95']}%")
        print(f"  mean over horizon : {summary['forecast_mean']}%\n")

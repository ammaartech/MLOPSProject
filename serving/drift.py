"""
Drift detection and automatic retraining.

This is the "continuously analyses" clause of the brief, and the thing
that separates a deployed system from a trained notebook. Two independent
signals:

    PERFORMANCE DRIFT   rolling prediction error against the error the
                        champion recorded at training time
    FEATURE DRIFT       population stability index between the training
                        distribution and the recent one

Why both
--------
They fail differently. Performance drift is the one that matters, but it
is only measurable once actuals arrive — at a 60-second horizon that is a
minute late, and if the collector stops it never arrives at all. Feature
drift is available immediately and needs no labels, but a shifted input
distribution does not always hurt accuracy. Performance drift decides;
feature drift is the early warning.

What happens on detection
-------------------------
A drift event is recorded, and when `drift.auto_retrain` is set the
training path runs again on current data. The retrained model then faces
the same promotion gate as any other challenger — drift triggers a
retrain, it does not authorise a deployment. A model that drifted and
whose replacement is still worse than persistence stays rejected.
"""

from datetime import datetime

import numpy as np
import pandas as pd

import config
from crud.query import execute_query
from serving.predictor import recent_predictions, resolve_champion


# ----------------------------------------------------------------------
# Feature drift
# ----------------------------------------------------------------------
def psi(reference, current, bins=10):
    """Population Stability Index between two distributions.

    Conventional reading: < 0.1 no meaningful shift, 0.1-0.25 moderate,
    > 0.25 major. Bin edges come from the REFERENCE distribution, since
    the question is how the new data sits against what the model learned.
    """
    reference = np.asarray(reference, dtype=float)
    current = np.asarray(current, dtype=float)
    reference = reference[np.isfinite(reference)]
    current = current[np.isfinite(current)]

    if len(reference) < bins or len(current) < bins:
        return None

    edges = np.percentile(reference, np.linspace(0, 100, bins + 1))
    edges[0], edges[-1] = -np.inf, np.inf
    edges = np.unique(edges)
    if len(edges) < 3:
        return 0.0

    reference_share = np.histogram(reference, bins=edges)[0] / len(reference)
    current_share = np.histogram(current, bins=edges)[0] / len(current)

    # A zero share makes the log term infinite; a small floor keeps the
    # index finite without materially changing a meaningful result.
    floor = 1e-6
    reference_share = np.clip(reference_share, floor, None)
    current_share = np.clip(current_share, floor, None)

    return float(np.sum((current_share - reference_share)
                        * np.log(current_share / reference_share)))


def feature_drift(df, target, window=None):
    """PSI between the earlier and most recent portions of the series."""
    window = window or config.get_int("drift.window")
    if df.empty or target not in df.columns or len(df) < window * 2:
        return {"psi": None, "reason": "not enough history"}

    values = pd.to_numeric(df[target], errors="coerce").dropna()
    reference, current = values.iloc[:-window], values.iloc[-window:]
    score = psi(reference, current)

    if score is None:
        severity = "unknown"
    elif score < 0.1:
        severity = "stable"
    elif score < 0.25:
        severity = "moderate"
    else:
        severity = "major"

    return {
        "psi": round(score, 4) if score is not None else None,
        "severity": severity,
        "reference_rows": len(reference),
        "current_rows": len(current),
    }


# ----------------------------------------------------------------------
# Performance drift
# ----------------------------------------------------------------------
def rolling_error(target, window=None):
    """Mean absolute error over the most recent scored predictions."""
    window = window or config.get_int("drift.window")
    frame = recent_predictions(target, limit=window, scored_only=True)
    if frame.empty:
        return {"window_mae": None, "n": 0}
    return {
        "window_mae": round(float(frame["abs_error"].mean()), 4),
        "worst": round(float(frame["abs_error"].max()), 4),
        "n": len(frame),
        "from_ts": str(frame["ts"].iloc[0]),
        "to_ts": str(frame["ts"].iloc[-1]),
    }


def reference_error(target):
    """The error the champion recorded when it was promoted."""
    champion = resolve_champion(target)
    if champion is None:
        return None, None
    return champion["mae"], champion["model_id"]


def check(target, df=None):
    """Decide whether `target` has drifted. Returns a decision dict."""
    threshold = config.get_float("drift.mae_ratio_threshold")
    min_samples = config.get_int("drift.min_samples")

    reference_mae, model_id = reference_error(target)
    rolling = rolling_error(target)
    decision = {
        "target": target,
        "model_id": model_id,
        "reference_mae": reference_mae,
        "window_mae": rolling["window_mae"],
        "n_scored": rolling["n"],
        "threshold": threshold,
        "drifted": False,
        "action": "none",
    }

    if df is not None:
        decision["feature_drift"] = feature_drift(df, target)

    if model_id is None:
        decision["reason"] = "no champion to monitor"
        return decision

    if rolling["n"] < min_samples:
        decision["reason"] = (f"only {rolling['n']} scored prediction(s); "
                              f"need {min_samples} before declaring drift")
        return decision

    if reference_mae is None or reference_mae <= 0:
        # A champion with a perfect training score (a constant series)
        # has no ratio to compute against. Any sustained error at all is
        # the signal instead.
        if rolling["window_mae"] and rolling["window_mae"] > 0:
            decision.update(
                drifted=True, ratio=None,
                reason=(f"champion recorded MAE {reference_mae} but recent "
                        f"error is {rolling['window_mae']}; the series is no "
                        f"longer constant"),
            )
        else:
            decision["reason"] = "champion is exact and remains exact"
        return decision

    ratio = rolling["window_mae"] / reference_mae
    decision["ratio"] = round(ratio, 3)

    if ratio > threshold:
        decision.update(
            drifted=True,
            reason=(f"rolling MAE {rolling['window_mae']} is {ratio:.2f}x the "
                    f"reference {reference_mae} (threshold {threshold}x) "
                    f"over {rolling['n']} predictions"),
        )
    else:
        decision["reason"] = (f"rolling MAE {rolling['window_mae']} is "
                              f"{ratio:.2f}x reference — within tolerance")
    return decision


# ----------------------------------------------------------------------
# Events and response
# ----------------------------------------------------------------------
def record_event(decision):
    execute_query(
        """
        INSERT INTO drift_events
            (target, detected_at, window_mae, reference_mae, ratio,
             threshold, action, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (decision["target"], datetime.now().isoformat(timespec="seconds"),
         decision.get("window_mae"), decision.get("reference_mae"),
         decision.get("ratio"), decision.get("threshold"),
         decision.get("action"), decision.get("reason")),
    )


def events(target=None, limit=50):
    if target:
        rows = execute_query(
            "SELECT target, detected_at, window_mae, reference_mae, ratio, "
            "action, detail FROM drift_events WHERE target = ? "
            "ORDER BY id DESC LIMIT ?", (target, int(limit)), fetch=True,
        )
    else:
        rows = execute_query(
            "SELECT target, detected_at, window_mae, reference_mae, ratio, "
            "action, detail FROM drift_events ORDER BY id DESC LIMIT ?",
            (int(limit),), fetch=True,
        )
    return pd.DataFrame(rows or [], columns=[
        "target", "detected_at", "window_mae", "reference_mae", "ratio",
        "action", "detail",
    ])


def monitor(df=None, auto_retrain=None):
    """Check every target, record events, retrain when configured."""
    from model.features import load_clean_frame

    if df is None:
        df = load_clean_frame()

    auto_retrain = (auto_retrain if auto_retrain is not None
                    else config.get_bool("drift.auto_retrain"))

    decisions, retrained = {}, []
    for target in config.get_json("features.targets"):
        decision = check(target, df)

        if decision["drifted"]:
            decision["action"] = "retrain" if auto_retrain else "alert_only"
            record_event(decision)
            if auto_retrain:
                retrained.append(target)
        decisions[target] = decision

    if retrained:
        decisions["_retrain"] = retrain(df, retrained)

    return decisions


def retrain(df, target_list):
    """Retrain and re-gate. The gate still decides what gets deployed."""
    from model.forecast import train_one
    from pipeline.etl import latest_run
    from tracking.mlflow_tracker import run_gate

    run = latest_run() or {}
    results = {
        target: train_one(df, target, run_id=run.get("run_id"),
                          data_fingerprint=run.get("data_fingerprint"),
                          verbose=False)
        for target in target_list
    }
    return run_gate(results)


def format_report(decisions):
    lines = ["=" * 82, "DRIFT MONITOR", "=" * 82]
    for target, d in decisions.items():
        if target == "_retrain":
            continue
        status = "DRIFTED" if d["drifted"] else "ok"
        lines.append(f"\n  {target:14s} [{status}]")
        lines.append(f"    reference MAE : {d['reference_mae']}   "
                     f"rolling MAE: {d['window_mae']}   "
                     f"scored: {d['n_scored']}")
        if d.get("ratio") is not None:
            lines.append(f"    ratio         : {d['ratio']}x "
                         f"(threshold {d['threshold']}x)")
        if d.get("feature_drift", {}).get("psi") is not None:
            fd = d["feature_drift"]
            lines.append(f"    feature PSI   : {fd['psi']} ({fd['severity']})")
        lines.append(f"    {d.get('reason', '')}")
        if d["drifted"]:
            lines.append(f"    -> action: {d['action']}")

    if "_retrain" in decisions:
        lines.append("\n  RETRAIN OUTCOME (the gate still decides):")
        for target, decision in decisions["_retrain"].items():
            lines.append(f"    {target:14s} {decision['verdict']}: "
                         f"{decision['reason'][:60]}")

    lines.append("\n" + "=" * 82)
    return "\n".join(lines)


if __name__ == "__main__":
    from model.features import load_clean_frame

    frame = load_clean_frame()
    if frame.empty:
        raise SystemExit("No cleaned data. Run: python -m pipeline.etl")

    print(format_report(monitor(frame)))

    print("\nFeature drift across the series (no labels needed):")
    for target in config.get_json("features.targets"):
        fd = feature_drift(frame, target)
        print(f"  {target:14s} PSI={fd.get('psi')} ({fd.get('severity')})")

    past = events()
    if not past.empty:
        print("\nRecorded drift events:")
        print(past.to_string(index=False))

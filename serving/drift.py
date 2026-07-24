"""
Drift detection and automatic retraining.

This is the "continuously analyses" clause of the brief, and the thing
that separates a deployed system from a trained notebook. Three signals,
each catching a failure the others miss:

    PERFORMANCE DRIFT    rolling prediction error against the error the
                         champion recorded at training time
    FEATURE DRIFT        population stability index between the training
                         distribution and the recent one (numeric inputs)
    CATEGORICAL DRIFT    shift in the mix of operating regimes, measured
                         on the ENCODED regime columns

Why all three
-------------
They fail differently. Performance drift is the one that ultimately
matters, but it is only measurable once actuals arrive — at a 60-second
horizon that is a minute late, and if the collector stops it never
arrives at all. Feature drift is available immediately and needs no
labels, but a shifted input distribution does not always hurt accuracy.
Categorical drift catches the specific event this project cares about
most: the load generator switching pattern moves the regime mix (more
`saturated`, less `idle`) long before the averaged error notices.
Performance drift decides; the other two are the early warning.

What happens on detection — the completed loop
----------------------------------------------
1. A drift event is written with the trigger that fired and its numbers.
2. If the target is inside its retrain COOLDOWN, the event is recorded as
   `suppressed` and nothing retrains. Drift persists until a better model
   exists, so without a cooldown one sustained shift would retrain on
   every monitor cycle.
3. Otherwise, when `drift.auto_retrain` is set, the training path runs
   again on current data and faces the same promotion gate as any other
   challenger. Drift triggers a retrain; it does not authorise a
   deployment. A model that drifted and whose replacement is still worse
   than persistence stays rejected.
4. The gate's verdict is written back onto the drift event as its
   OUTCOME — `promoted`, `rejected`, or `alert_only` — so the event log
   records what actually happened, not only what was attempted. On this
   data the honest outcome is usually `rejected`, and the log says so.

Encoding
--------
Categorical drift reuses `pipeline.encode`: the regime is turned into the
same fixed-vocabulary columns the model trains on, and two windows are
compared as shares over that vocabulary. Because the vocabulary is fixed,
a window that happens to contain no `idle` rows still produces a
comparable vector rather than a shorter one.
"""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config
from crud.query import execute_insert, execute_query
from pipeline import encode as encode_mod
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
# Categorical drift — the regime mix, measured on the encoded columns
# ----------------------------------------------------------------------
def categorical_drift(df, column="regime", window=None):
    """PSI between the earlier and recent category shares of `column`.

    Uses `pipeline.encode.category_shares`, so the comparison runs over
    the fixed vocabulary the model trains on. A regime that appears in
    neither window still occupies a slot, which keeps the two share
    vectors aligned — the alignment a raw `value_counts` would lose.

    This is the signal that fires when the load generator changes pattern:
    the mix moves toward `saturated` well before averaged error reacts.
    """
    window = window or config.get_int("drift.window")
    if df is None or df.empty or len(df) < window * 2:
        return {"psi": None, "reason": "not enough history"}

    reference = df.iloc[:-window]
    current = df.iloc[-window:]

    ref_shares = encode_mod.category_shares(reference, column)
    cur_shares = encode_mod.category_shares(current, column)
    if not ref_shares or not cur_shares:
        return {"psi": None, "reason": f"'{column}' not available to encode"}

    # PSI over the shared, fixed vocabulary. Same formula as the numeric
    # psi() above, but the bins are the categories rather than percentiles.
    floor = 1e-6
    score = 0.0
    for category in ref_shares:
        r = max(ref_shares[category], floor)
        c = max(cur_shares.get(category, 0.0), floor)
        score += (c - r) * np.log(c / r)
    score = float(score)

    if score < 0.1:
        severity = "stable"
    elif score < 0.25:
        severity = "moderate"
    else:
        severity = "major"

    return {
        "psi": round(score, 4),
        "severity": severity,
        "reference_shares": {k: round(v, 3) for k, v in ref_shares.items()},
        "current_shares": {k: round(v, 3) for k, v in cur_shares.items()},
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
    """Decide whether `target` has drifted, and on which signal.

    Evaluates all three signals and sets `trigger` to the FIRST that
    fired, in priority order: performance first (it is the one that
    directly measures harm), then categorical, then numeric feature
    drift. `drifted` is true if any fired. The others are still recorded,
    so the event log shows the whole picture rather than only the winner.
    """
    threshold = config.get_float("drift.mae_ratio_threshold")
    min_samples = config.get_int("drift.min_samples")
    psi_threshold = config.get_float("drift.psi_threshold")
    allow_feature_trigger = config.get_bool("drift.retrain_on_feature_drift")

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
        "trigger": None,
        "action": "none",
        "reasons": [],
    }

    # --- the two label-free signals, always computed when data is given --
    if df is not None:
        num = feature_drift(df, target)
        decision["feature_drift"] = num
        cat = categorical_drift(df)
        decision["categorical_drift"] = cat
        decision["psi"] = num.get("psi")

    if model_id is None:
        decision["reason"] = "no champion to monitor"
        return decision

    # --- performance drift (highest priority; needs actuals) -------------
    performance_drifted = False
    if rolling["n"] < min_samples:
        decision["reasons"].append(
            f"performance: only {rolling['n']} scored prediction(s), need "
            f"{min_samples} — not evaluated"
        )
    elif reference_mae is None or reference_mae <= 0:
        # A champion with a perfect training score (a constant series) has
        # no ratio to compute against. Any sustained error at all is the
        # signal instead.
        if rolling["window_mae"] and rolling["window_mae"] > 0:
            performance_drifted = True
            decision["ratio"] = None
            decision["reasons"].append(
                f"performance: champion recorded MAE {reference_mae} but "
                f"recent error is {rolling['window_mae']}; series no longer "
                f"constant"
            )
        else:
            decision["reasons"].append("performance: champion exact and remains exact")
    else:
        ratio = rolling["window_mae"] / reference_mae
        decision["ratio"] = round(ratio, 3)
        if ratio > threshold:
            performance_drifted = True
            decision["reasons"].append(
                f"performance: rolling MAE {rolling['window_mae']} is "
                f"{ratio:.2f}x reference {reference_mae} (limit {threshold}x)"
            )
        else:
            decision["reasons"].append(
                f"performance: {ratio:.2f}x reference — within tolerance"
            )

    # --- categorical drift (the regime mix moved) ------------------------
    cat = decision.get("categorical_drift", {})
    categorical_drifted = (
        allow_feature_trigger and cat.get("psi") is not None
        and cat["psi"] > psi_threshold
    )
    if cat.get("psi") is not None:
        decision["reasons"].append(
            f"regime mix: PSI {cat['psi']} ({cat['severity']})"
            + (f" > {psi_threshold} — SHIFTED" if categorical_drifted else "")
        )

    # --- numeric feature drift -------------------------------------------
    num = decision.get("feature_drift", {})
    feature_drifted = (
        allow_feature_trigger and num.get("psi") is not None
        and num["psi"] > psi_threshold
    )
    if num.get("psi") is not None:
        decision["reasons"].append(
            f"input dist: PSI {num['psi']} ({num['severity']})"
            + (f" > {psi_threshold} — SHIFTED" if feature_drifted else "")
        )

    # --- combine, priority-ordered ---------------------------------------
    if performance_drifted:
        decision["trigger"] = "performance"
    elif categorical_drifted:
        decision["trigger"] = "categorical"
    elif feature_drifted:
        decision["trigger"] = "feature"

    decision["drifted"] = decision["trigger"] is not None
    decision["reason"] = "; ".join(decision["reasons"])
    return decision


# ----------------------------------------------------------------------
# Events and response
# ----------------------------------------------------------------------
def record_event(decision):
    """Write one drift event and return its id, so the outcome of any
    retrain it triggers can be written back onto the same row."""
    cat = decision.get("categorical_drift", {})
    return execute_insert(
        """
        INSERT INTO drift_events
            (target, detected_at, window_mae, reference_mae, ratio,
             threshold, action, detail, psi, trigger)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (decision["target"], datetime.now().isoformat(timespec="seconds"),
         decision.get("window_mae"), decision.get("reference_mae"),
         decision.get("ratio"), decision.get("threshold"),
         decision.get("action"), decision.get("reason"),
         decision.get("psi") if decision.get("psi") is not None
         else cat.get("psi"),
         decision.get("trigger")),
    )


def update_event_outcome(event_id, outcome, new_model_id=None,
                         feature_version=None):
    """Record what a triggered retrain actually produced.

    `action` said what the monitor decided to do; this fills in what the
    promotion gate then did with the result. The two differ on exactly the
    case that matters: a retrain ran, and the gate rejected it anyway.
    """
    if event_id is None:
        return
    execute_query(
        """
        UPDATE drift_events
           SET outcome = ?, new_model_id = ?, feature_version = ?,
               resolved_at = ?
         WHERE id = ?
        """,
        (outcome, new_model_id, feature_version,
         datetime.now().isoformat(timespec="seconds"), event_id),
    )


def seconds_since_last_retrain(target):
    """Seconds since this target last actually retrained, or None.

    'Retrained' means an event whose action was `retrain`, regardless of
    whether the gate then promoted the result — a rejected retrain still
    spent the compute and still starts the cooldown.
    """
    rows = execute_query(
        "SELECT detected_at FROM drift_events "
        "WHERE target = ? AND action = 'retrain' "
        "ORDER BY id DESC LIMIT 1",
        (target,), fetch=True,
    )
    if not rows:
        return None
    try:
        last = datetime.fromisoformat(rows[0][0])
    except (TypeError, ValueError):
        return None
    return (datetime.now() - last).total_seconds()


def in_cooldown(target):
    """Whether `target` retrained too recently to retrain again."""
    cooldown = config.get_int("drift.retrain_cooldown_sec")
    if cooldown <= 0:
        return False, None
    elapsed = seconds_since_last_retrain(target)
    if elapsed is None:
        return False, None
    remaining = cooldown - elapsed
    return remaining > 0, max(0, remaining)


def events(target=None, limit=50):
    columns = ("target, detected_at, trigger, psi, ratio, action, outcome, "
               "new_model_id")
    if target:
        rows = execute_query(
            f"SELECT {columns} FROM drift_events WHERE target = ? "
            "ORDER BY id DESC LIMIT ?", (target, int(limit)), fetch=True,
        )
    else:
        rows = execute_query(
            f"SELECT {columns} FROM drift_events ORDER BY id DESC LIMIT ?",
            (int(limit),), fetch=True,
        )
    return pd.DataFrame(rows or [], columns=[
        "target", "detected_at", "trigger", "psi", "ratio", "action",
        "outcome", "new_model_id",
    ])


def monitor(df=None, auto_retrain=None):
    """Check every target, record events, retrain when allowed.

    The full decision path per target:

        not drifted            -> nothing recorded
        drifted, no retrain    -> event recorded, action `alert_only`
        drifted, in cooldown   -> event recorded, action `suppressed`
        drifted, retrain ok    -> event recorded action `retrain`, model
                                   retrained, gate verdict written back as
                                   the event's outcome
    """
    from model.features import load_clean_frame

    if df is None:
        df = load_clean_frame()

    auto_retrain = (auto_retrain if auto_retrain is not None
                    else config.get_bool("drift.auto_retrain"))

    decisions = {}
    to_retrain = []          # (target, event_id) pairs actually retraining

    for target in config.get_json("features.targets"):
        decision = check(target, df)

        if decision["drifted"]:
            if not auto_retrain:
                decision["action"] = "alert_only"
            else:
                cooling, remaining = in_cooldown(target)
                if cooling:
                    decision["action"] = "suppressed"
                    decision["cooldown_remaining_sec"] = int(remaining)
                    decision["reason"] += (
                        f"; retrain suppressed, {int(remaining)}s of cooldown "
                        f"remaining"
                    )
                else:
                    decision["action"] = "retrain"

            event_id = record_event(decision)
            decision["event_id"] = event_id
            if decision["action"] == "retrain":
                to_retrain.append((target, event_id))

        decisions[target] = decision

    if to_retrain:
        decisions["_retrain"] = retrain(df, to_retrain)

    return decisions


def retrain(df, targets_with_events):
    """Retrain each drifted target, re-gate, and write the gate's verdict
    back onto the drift event that triggered it.

    The gate still decides what gets deployed. Retraining because of drift
    does not lower the bar: the fresh model is measured against the same
    baseline ladder and cross-validation as any other challenger, and if
    it still loses to persistence it is rejected and the incumbent stays.
    That rejection is written onto the event as its outcome, so the log is
    honest about drift that could not be fixed by retraining.
    """
    from model.forecast import train_one
    from pipeline.etl import latest_run
    from tracking.mlflow_tracker import run_gate

    run = latest_run() or {}
    event_by_target = dict(targets_with_events)

    results = {
        target: train_one(df, target, run_id=run.get("run_id"),
                          data_fingerprint=run.get("data_fingerprint"),
                          verbose=False)
        for target, _ in targets_with_events
    }

    decisions = run_gate(results)

    for target, decision in decisions.items():
        event_id = event_by_target.get(target)
        if event_id is None:
            continue
        outcome = "promoted" if decision.get("promote") else "rejected"
        result = results.get(target, {})
        update_event_outcome(
            event_id, outcome,
            new_model_id=result.get("model_id"),
            feature_version=result.get("feature_version"),
        )
        decision["drift_event_id"] = event_id

    return decisions


def format_report(decisions):
    lines = ["=" * 82, "DRIFT MONITOR", "=" * 82]
    for target, d in decisions.items():
        if target == "_retrain":
            continue
        status = f"DRIFTED via {d['trigger']}" if d["drifted"] else "ok"
        lines.append(f"\n  {target:14s} [{status}]")
        lines.append(f"    performance   : reference MAE {d['reference_mae']}, "
                     f"rolling MAE {d['window_mae']}, scored {d['n_scored']}"
                     + (f", {d['ratio']}x (limit {d['threshold']}x)"
                        if d.get("ratio") is not None else ""))
        if d.get("feature_drift", {}).get("psi") is not None:
            fd = d["feature_drift"]
            lines.append(f"    input dist    : PSI {fd['psi']} ({fd['severity']})")
        if d.get("categorical_drift", {}).get("psi") is not None:
            cd = d["categorical_drift"]
            lines.append(f"    regime mix    : PSI {cd['psi']} ({cd['severity']})  "
                         f"{cd.get('reference_shares')} -> "
                         f"{cd.get('current_shares')}")
        if d["drifted"]:
            action = d["action"]
            if action == "suppressed":
                action += f" ({d.get('cooldown_remaining_sec', 0)}s cooldown left)"
            lines.append(f"    -> action     : {action}")

    if "_retrain" in decisions:
        lines.append("\n  RETRAIN OUTCOME (the gate still decides):")
        for target, decision in decisions["_retrain"].items():
            outcome = "PROMOTED" if decision.get("promote") else "REJECTED"
            lines.append(f"    {target:14s} {outcome}: "
                         f"{decision['reason'][:64]}")

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

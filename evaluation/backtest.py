"""
Walk-forward backtest of the allocation decision.

A single snapshot answers "what would we allocate right now". It cannot
answer "what would this policy have cost over an hour, and how often
would it have failed". Only replay can, and replay is the difference
between a projected saving and a measured one.

Method
------
Walk the cleaned series one decision at a time. At each step the policy
sees ONLY the rows before it, decides an allocation, and is then charged
for the following `horizon` samples and judged on whether demand exceeded
the allocation during them. Nothing downstream of the decision point is
visible when the decision is made.

Policies compared on identical data
-----------------------------------
    static_100        always 100%. The over-provisioned default.
    reactive_p95      trailing P95 of recent actuals, plus headroom.
                      NO MODEL AT ALL — the control that shows how much
                      of any saving comes from the allocation policy
                      rather than from forecasting.
    predictive        the champion's forecast, plus headroom, then the
                      safety floor.
    predictive_nofloor  the same without the safety floor, to price what
                      the floor costs.
    oracle            perfect foresight: exactly the peak of the window
                      it is about to be charged for. Not achievable —
                      it bounds how much value forecasting could add.

The gap between `reactive_p95` and `predictive` is the value the model
contributes. The gap between `predictive` and `oracle` is what a perfect
model would add on top. Reporting both keeps the result honest in either
direction.
"""

import numpy as np
import pandas as pd

import config
from service.cost_model import (
    apply_safety_floor, breach_stats, resource_monthly_cost, static_cost,
)


# ----------------------------------------------------------------------
# Policies — each returns an allocation percentage from history alone
# ----------------------------------------------------------------------
def policy_static(history, target, **_):
    return 100.0


def policy_reactive_p95(history, target, **_):
    """Trailing P95 plus headroom. Uses no model whatsoever."""
    window = config.get_int("policy.safety_window")
    recent = history[target].tail(window).to_numpy(dtype=float)
    if recent.size == 0:
        return 100.0
    return float(min(100.0, np.percentile(recent, 95)
                     * (1 + config.get_float("policy.headroom"))))


def policy_predictive(history, target, forecast=None, floor=True, **_):
    """Forecast P95 plus headroom, then the safety floor."""
    if forecast is None or len(forecast) == 0:
        return policy_reactive_p95(history, target)

    candidate = float(min(100.0, np.percentile(forecast, 95)
                          * (1 + config.get_float("policy.headroom"))))
    if not floor:
        return candidate

    window = config.get_int("policy.safety_window")
    recent = history[target].tail(window).to_numpy(dtype=float)
    floored, _ = apply_safety_floor(candidate, recent)
    return floored


def policy_predictive_nofloor(history, target, forecast=None, **_):
    return policy_predictive(history, target, forecast=forecast, floor=False)


def policy_oracle(history, target, future=None, **_):
    """Perfect foresight. Unachievable; bounds the value of forecasting."""
    if future is None or len(future) == 0:
        return 100.0
    return float(min(100.0, np.max(future)))


POLICIES = {
    "static_100": policy_static,
    "reactive_p95": policy_reactive_p95,
    "predictive": policy_predictive,
    "predictive_nofloor": policy_predictive_nofloor,
    "oracle": policy_oracle,
}


# ----------------------------------------------------------------------
# Forecasting inside the replay
# ----------------------------------------------------------------------
def _forecast_from_history(history, target, steps, model=None, features=None):
    """The champion's view of the next `steps` samples, from history only.

    With a persistence champion this is a flat line at the current value,
    which is exactly what persistence claims. With a trained champion it
    is an iterative multi-step rollout.
    """
    if model is None or features is None:
        current = float(history[target].iloc[-1])
        return np.full(steps, current)

    from model.features import build_serving_row

    working = history.copy()
    cadence = config.get_int("pipeline.nominal_cadence_sec")
    predictions = []

    for _ in range(steps):
        row, _info = build_serving_row(working, target)
        if row is None:
            break
        missing = [c for c in features if c not in row.columns]
        if missing:
            break
        value = float(model.predict(row[features])[0])
        predictions.append(value)

        next_row = working.iloc[[-1]].copy()
        next_row["ts"] = (pd.to_datetime(working["ts"].iloc[-1])
                          + pd.Timedelta(seconds=cadence))
        next_row[target] = value
        working = pd.concat([working, next_row], ignore_index=True)

    if not predictions:
        return np.full(steps, float(history[target].iloc[-1]))
    return np.asarray(predictions, dtype=float)


# ----------------------------------------------------------------------
# The replay
# ----------------------------------------------------------------------
def run(df, target, warmup=None, horizon=None, use_model=False,
        retrain_every=25, log_predictions=False, verbose=False):
    """Replay every allocation decision for one target."""
    horizon = horizon or config.get_int("model.forecast_horizon")
    warmup = warmup or max(config.get_int("policy.safety_window"),
                           max(config.get_json("features.lags")) + 5)

    if len(df) < warmup + horizon * 2:
        return {"target": target,
                "error": f"need at least {warmup + horizon * 2} rows, have {len(df)}"}

    model, features = None, None
    records = []
    steps = 0

    for start in range(warmup, len(df) - horizon, horizon):
        history = df.iloc[:start]
        future = df[target].iloc[start:start + horizon].to_numpy(dtype=float)
        if future.size == 0:
            continue

        # --- optionally refit on the data available at this point -------
        if use_model and steps % retrain_every == 0:
            model, features = _fit(history, target)

        forecast = _forecast_from_history(history, target, horizon,
                                          model=model, features=features)

        if log_predictions:
            _log(df, target, start, forecast)

        row = {"step": steps, "ts": df["ts"].iloc[start],
               "actual_peak": round(float(np.max(future)), 3)}

        for name, policy in POLICIES.items():
            allocation = policy(history, target, forecast=forecast, future=future)
            stats = breach_stats(future, allocation)
            row[f"{name}_alloc"] = round(allocation, 3)
            row[f"{name}_breaches"] = stats["breaches"]
            row[f"{name}_samples"] = stats["samples"]

        records.append(row)
        steps += 1

    if not records:
        return {"target": target, "error": "no decision steps produced"}

    trace = pd.DataFrame(records)
    summary = _summarise(trace, target)

    if verbose:
        print(format_target(target, summary, len(trace)))

    return {"target": target, "trace": trace, "summary": summary,
            "decisions": len(trace), "horizon": horizon, "warmup": warmup,
            "used_model": use_model}


def _fit(history, target):
    """Train on the data available at this point in the replay."""
    from sklearn.ensemble import GradientBoostingRegressor

    from model.features import build_features

    X, y, _meta = build_features(history, target)
    if len(X) < 30:
        return None, None
    model = GradientBoostingRegressor(**config.get_json("model.params"))
    model.fit(X, y)
    return model, list(X.columns)


def _log(df, target, start, forecast):
    """Write the first forecast step to `predictions`, for drift scoring."""
    from serving.predictor import log_prediction, resolve_champion

    champion = resolve_champion(target)
    if champion is None or start >= len(df):
        return
    log_prediction(target, champion["model_id"],
                   pd.to_datetime(df["ts"].iloc[start]), float(forecast[0]))


def _summarise(trace, target):
    """Cost and reliability per policy, over the whole replay."""
    baseline_total = None
    rows = []

    for name in POLICIES:
        allocations = trace[f"{name}_alloc"].to_numpy(dtype=float)
        breaches = int(trace[f"{name}_breaches"].sum())
        samples = int(trace[f"{name}_samples"].sum())

        # Time-weighted: every decision governs an equal-length window,
        # so the mean allocation is what the month would have cost.
        mean_allocation = float(np.mean(allocations))
        cost = resource_monthly_cost(target, mean_allocation)
        if name == "static_100":
            baseline_total = cost

        rows.append({
            "policy": name,
            "mean_alloc_pct": round(mean_allocation, 2),
            "min_alloc_pct": round(float(np.min(allocations)), 2),
            "monthly_cost": cost,
            "breaches": breaches,
            "samples": samples,
            "breach_rate": round(100.0 * breaches / samples, 3) if samples else 0.0,
        })

    max_breach = config.get_float("policy.max_breach_rate")
    for row in rows:
        row["savings_vs_static"] = round(baseline_total - row["monthly_cost"], 2)
        row["savings_pct"] = (round(100.0 * row["savings_vs_static"] / baseline_total, 2)
                              if baseline_total else 0.0)
        row["sla_met"] = row["breach_rate"] <= max_breach

    return pd.DataFrame(rows)


def run_all(df=None, use_model=False, log_predictions=False):
    from model.features import load_clean_frame

    if df is None:
        df = load_clean_frame()
    return {
        target: run(df, target, use_model=use_model,
                    log_predictions=log_predictions)
        for target in config.get_json("features.targets")
    }


def combined(results):
    """Whole-node cost per policy, summing the three resources.

    Breach rate is the WORST across resources, never the pooled average.
    Pooling lets memory's zero breaches dilute CPU's failures — an earlier
    version reported the node at 2.58% and SLA-compliant while CPU alone
    was breaching 7.73%. An SLA is violated when any one resource violates
    it, so the maximum is the only defensible aggregate.
    """
    totals = {}
    for target, result in results.items():
        if "error" in result:
            continue
        for _, row in result["summary"].iterrows():
            entry = totals.setdefault(row["policy"], {
                "policy": row["policy"], "monthly_cost": 0.0,
                "breaches": 0, "worst_breach_rate": 0.0,
                "worst_resource": None,
            })
            entry["monthly_cost"] += row["monthly_cost"]
            entry["breaches"] += row["breaches"]
            if row["breach_rate"] > entry["worst_breach_rate"]:
                entry["worst_breach_rate"] = row["breach_rate"]
                entry["worst_resource"] = target

    if not totals:
        return pd.DataFrame()

    frame = pd.DataFrame(totals.values())
    frame["monthly_cost"] = frame["monthly_cost"].round(2)
    baseline = float(frame.loc[frame["policy"] == "static_100", "monthly_cost"].iloc[0])
    frame["savings_vs_static"] = (baseline - frame["monthly_cost"]).round(2)
    frame["savings_pct"] = (100.0 * frame["savings_vs_static"] / baseline).round(2)
    frame["sla_met"] = (frame["worst_breach_rate"]
                        <= config.get_float("policy.max_breach_rate"))
    return frame.sort_values("monthly_cost").reset_index(drop=True)


# ----------------------------------------------------------------------
def format_target(target, summary, decisions):
    lines = [
        f"\n  {target}  ({decisions} sequential allocation decisions)",
        "    " + summary.to_string(index=False).replace("\n", "\n    "),
    ]
    return "\n".join(lines)


def format_report(results):
    lines = ["=" * 96, "WALK-FORWARD BACKTEST", "=" * 96]
    for target, result in results.items():
        if "error" in result:
            lines.append(f"\n  {target}: {result['error']}")
            continue
        lines.append(format_target(target, result["summary"],
                                   result["decisions"]))

    node = combined(results)
    if not node.empty:
        lines += [
            "",
            "-" * 96,
            "  WHOLE NODE — all three resources",
            "    " + node.to_string(index=False).replace("\n", "\n    "),
        ]
    lines.append("\n" + "=" * 96)
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    from model.features import load_clean_frame

    frame = load_clean_frame()
    if frame.empty:
        raise SystemExit("No cleaned data. Run: python -m pipeline.etl")

    use_model = "--model" in sys.argv
    print(f"Replaying {len(frame)} samples "
          f"({'refitting a model as it goes' if use_model else 'champion policy'})...")

    results = run_all(frame, use_model=use_model, log_predictions=True)
    print(format_report(results))
    print(f"\n  static baseline (whole node): ${static_cost()['total']}/month")

"""
Recommender — forecasts into allocation decisions and dollar figures.

Per resource:

    1. Forecast the next horizon using the CHAMPION predictor.
    2. Take the P95 of that trajectory — the predicted near-peak.
    3. Add headroom -> the lean, forecast-driven candidate.
    4. Apply the safety floor: raise the allocation until the breach rate
       on recent ACTUAL demand complies with the SLA.
    5. Price it, and compare against static 100% provisioning.

Steps 3 and 4 pull in opposite directions, and the gap between them is
the finding rather than a defect. The forecast proposes what is efficient;
the floor enforces what is safe. When they disagree sharply, the system is
telling you that this signal is not predictable enough to allocate against
aggressively.

A caution this module now states rather than hides
--------------------------------------------------
A single recommendation is a snapshot. `evaluation/backtest.py` replays
the same policy across the whole series and measures what it would
actually have cost and breached. On this data the snapshot and the replay
disagree, and the replay is the number to trust. `print_report` prints
both so the difference is never invisible.
"""

from datetime import datetime

import numpy as np

import config
from crud.query import execute_query
from service.cost_model import (
    apply_safety_floor, breach_stats, cost_for_allocation, resource_map,
    resource_monthly_cost, static_cost, sweep, sla_minimum,
)


def recommend_percent(target, df=None):
    """The allocation for one resource, forecast-driven and safety-floored."""
    from model.features import load_clean_frame
    from serving.predictor import forecast_horizon

    if df is None:
        df = load_clean_frame()
    if df.empty:
        return None

    trajectory, meta = forecast_horizon(target, df=df)
    if trajectory.empty:
        return None

    forecast_p95 = float(meta["forecast_p95"])
    forecast_peak = float(meta["forecast_peak"])
    headroom = config.get_float("policy.headroom")

    candidate = float(min(100.0, forecast_p95 * (1 + headroom)))

    window = config.get_int("policy.safety_window")
    recent = df[target].tail(window).to_numpy(dtype=float)
    floored, floor_info = apply_safety_floor(candidate, recent)

    stats = breach_stats(recent, floored)
    label, capacity, _ = resource_map()[target]

    return {
        "target": target,
        "predictor": meta.get("predictor"),
        "model_id": meta.get("model_id"),
        "forecast_peak": round(forecast_peak, 2),
        "forecast_p95": round(forecast_p95, 2),
        "forecast_alloc": round(candidate, 2),
        "recommended_percent": floored,
        "floored": floor_info["floored"],
        "floor_reason": floor_info["reason"],
        "units": round(floored / 100.0 * capacity, 2),
        "unit_label": label,
        "monthly_cost": resource_monthly_cost(target, floored),
        "static_cost": resource_monthly_cost(target, 100.0),
        "breach_rate": stats["breach_rate"],
        "breach_episodes": stats["episodes"],
    }


def build_recommendation(df=None, persist=True):
    """Allocation and cost across every resource."""
    from model.features import load_clean_frame

    if df is None:
        df = load_clean_frame()

    recommendations, allocation = {}, {}
    for target in config.get_json("features.targets"):
        result = recommend_percent(target, df)
        if result is None:
            continue
        recommendations[target] = result
        allocation[target] = result["recommended_percent"]

    baseline = static_cost()
    predictive = cost_for_allocation(allocation)
    savings = round(baseline["total"] - predictive["total"], 2)

    if persist:
        _persist(recommendations)

    return {
        "recommendations": recommendations,
        "allocation": allocation,
        "static_cost": baseline,
        "predictive_cost": predictive,
        "monthly_savings": savings,
        "savings_percent": (round(100.0 * savings / baseline["total"], 2)
                            if baseline["total"] else 0.0),
    }


def _persist(recommendations):
    now = datetime.now().isoformat(timespec="seconds")
    for target, r in recommendations.items():
        execute_query(
            """
            INSERT INTO recommendations
                (ts, target, forecast_p95, wanted_percent, floored_percent,
                 allocated_units, unit_label, monthly_cost, static_cost,
                 breach_rate, model_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now, target, r["forecast_p95"], r["forecast_alloc"],
             r["recommended_percent"], r["units"], r["unit_label"],
             r["monthly_cost"], r["static_cost"], r["breach_rate"],
             r["model_id"]),
        )


def sla_check(target, allocation_percent, window=None, df=None):
    """How often recent actual demand exceeded the allocation."""
    from model.features import load_clean_frame

    if df is None:
        df = load_clean_frame()
    if df.empty:
        return None
    window = window or config.get_int("policy.safety_window")
    return breach_stats(df[target].tail(window).to_numpy(dtype=float),
                        allocation_percent)


def tradeoff_curve(target, df=None):
    """Cost against breach rate, with the cheapest compliant point."""
    from model.features import load_clean_frame

    if df is None:
        df = load_clean_frame()
    if df.empty:
        return None, None
    curve = sweep(target, df[target].to_numpy(dtype=float))
    return curve, sla_minimum(curve)


def print_report(df=None):
    result = build_recommendation(df)

    print("\n" + "=" * 82)
    print("ALLOCATION RECOMMENDATION")
    print("=" * 82)

    for target, r in result["recommendations"].items():
        print(f"\n{target}   [{r['predictor']}]")
        print(f"  forecast P95 / peak  : {r['forecast_p95']}% / {r['forecast_peak']}%")
        print(f"  forecast wants       : {r['forecast_alloc']}%  (lean, pre-safety)")
        print(f"  recommended          : {r['recommended_percent']}%  "
              f"(~{r['units']} {r['unit_label']})")
        if r["floored"]:
            print(f"  safety floor         : {r['floor_reason']}")
        print(f"  breaches on recent   : {r['breach_rate']}% of samples "
              f"in {r['breach_episodes']} episode(s)")
        print(f"  monthly cost         : ${r['monthly_cost']}  "
              f"(static ${r['static_cost']})")

    print("\n" + "-" * 82)
    print("SNAPSHOT COST COMPARISON")
    print(f"  static (100%)        : ${result['static_cost']['total']}/month")
    print(f"  predictive           : ${result['predictive_cost']['total']}/month")
    print(f"  savings              : ${result['monthly_savings']} "
          f"({result['savings_percent']}%)")
    print("\n  NOTE: this is a single instant. The measured figure comes from")
    print("        python -m evaluation.backtest, which replays every decision.")
    print("=" * 82)
    return result


if __name__ == "__main__":
    from model.features import load_clean_frame

    frame = load_clean_frame()
    if frame.empty:
        raise SystemExit("No cleaned data. Run: python -m pipeline.etl")

    print_report(frame)

    print("\nCOST vs SLA TRADEOFF (cheapest allocation meeting the SLA)")
    for target in config.get_json("features.targets"):
        curve, minimum = tradeoff_curve(target, frame)
        if minimum:
            print(f"  {target:14s} {minimum['allocation_percent']:6.2f}% "
                  f"-> ${minimum['monthly_cost']:7.2f}/mo at "
                  f"{minimum['breach_rate']}% breaches")

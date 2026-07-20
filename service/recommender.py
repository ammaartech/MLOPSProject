"""
Recommender: turns forecasts into allocation decisions + dollar figures.

Pipeline per resource:
  1. Forecast demand over a horizon (multi-step, iterative).
  2. Take P95 of the FORECASTED trajectory -> predicted near-peak.
  3. Add a headroom buffer -> the efficient, forecast-driven candidate.
  4. Apply a SAFETY FLOOR: raise the allocation until the breach rate on
     recent ACTUAL demand falls under an acceptable limit. This stops us
     under-provisioning when the forecast is too optimistic.
  5. Price it with real AWS rates; compare vs a STATIC (100%) baseline.
  6. Report savings + the SLA breach rate we actually achieve.

Forecast-driven for efficiency, safety-floored for reliability:
"cut cost WHILE holding SLA breaches under the limit."
"""

import numpy as np

from config import HEADROOM, NODE_VCPUS, NODE_RAM_GB, NODE_STORAGE_GB
from model.features import load_dataframe, TARGETS
from model.horizon import horizon_summary
from service.cost_model import monthly_cost


# Map each forecast target to its node-capacity total + a label
RESOURCE_MAP = {
    "cpu_percent":  ("vCPUs",      NODE_VCPUS),
    "mem_percent":  ("GB RAM",     NODE_RAM_GB),
    "disk_percent": ("GB storage", NODE_STORAGE_GB),
}

# Max fraction of recent samples allowed to exceed the allocation
MAX_BREACH_RATE = 5.0  # percent


def recommend_percent(target, max_breach_rate=MAX_BREACH_RATE):
    """
    Forecast-driven allocation, floored by a safety constraint.
    The forecast lets us go lean WHEN safe; a historical safety floor
    (recent ACTUAL demand) prevents under-provisioning when the forecast
    is too optimistic. We raise the allocation until the breach rate on
    recent data falls under max_breach_rate.
    """
    summary = horizon_summary(target)
    if summary is None:
        return None

    forecast_p95 = summary["forecast_p95"]
    forecast_peak = summary["forecast_peak"]

    # forecast-driven candidate (efficient)
    forecast_alloc = min(100.0, forecast_p95 * (1 + HEADROOM))

    # safety floor: raise allocation until breaches on recent actuals <= target
    df = load_dataframe()
    actual = df[target].tail(100).values if not df.empty else np.array([])

    safe_alloc = forecast_alloc
    if len(actual) > 0:
        for pct in np.arange(forecast_alloc, 100.5, 0.5):
            breach_rate = np.sum(actual > pct) / len(actual) * 100
            if breach_rate <= max_breach_rate:
                safe_alloc = round(float(pct), 2)
                break
        else:
            safe_alloc = 100.0

    return {
        "target": target,
        "forecast_peak": forecast_peak,
        "forecast_p95": forecast_p95,
        "forecast_alloc": round(forecast_alloc, 2),  # what forecast alone wanted
        "recommended_percent": safe_alloc,           # after safety floor
    }


def build_recommendation():
    """
    Full recommendation across all resources, with cost comparison:
      STATIC baseline = provision 100% of the node (over-provisioned,
      the risk-averse default) vs PREDICTIVE = our recommended %.
    """
    rec = {}
    pred_pct = {}

    for tgt in TARGETS:
        r = recommend_percent(tgt)
        if r is None:
            continue
        rec[tgt] = r
        pred_pct[tgt] = r["recommended_percent"]

    # Cost of the static (fully provisioned) node
    static_cost = monthly_cost(NODE_VCPUS, NODE_RAM_GB, NODE_STORAGE_GB)

    # Cost of the predictive allocation
    pred_cost = monthly_cost(
        (pred_pct.get("cpu_percent", 100) / 100) * NODE_VCPUS,
        (pred_pct.get("mem_percent", 100) / 100) * NODE_RAM_GB,
        (pred_pct.get("disk_percent", 100) / 100) * NODE_STORAGE_GB,
    )

    savings = round(static_cost["total"] - pred_cost["total"], 2)
    savings_pct = round(savings / static_cost["total"] * 100, 1) if static_cost["total"] else 0

    return {
        "recommendations": rec,
        "static_cost": static_cost,
        "predictive_cost": pred_cost,
        "monthly_savings": savings,
        "savings_percent": savings_pct,
    }


def sla_check(target, recommended_percent, window=100):
    """
    SLA guardrail: over the recent window, how often did actual demand
    exceed our recommended allocation? (A 'breach' = under-provisioned.)
    Proves we cut cost WITHOUT hurting performance.
    """
    df = load_dataframe()
    if df.empty:
        return None
    actual = df[target].tail(window).values
    breaches = int(np.sum(actual > recommended_percent))
    breach_rate = round(breaches / len(actual) * 100, 1)
    return {"breaches": breaches, "samples": len(actual), "breach_rate": breach_rate}


def print_report():
    result = build_recommendation()
    print("\n==================== ALLOCATION RECOMMENDATION ====================")
    for tgt, r in result["recommendations"].items():
        label, total = RESOURCE_MAP[tgt]
        units = round(r["recommended_percent"] / 100 * total, 2)
        sla = sla_check(tgt, r["recommended_percent"])
        print(f"\n{tgt}")
        print(f"  forecast peak (60s)  : {r['forecast_peak']}%")
        print(f"  forecast P95 (60s)   : {r['forecast_p95']}%")
        print(f"  forecast-only alloc  : {r['forecast_alloc']}%  (efficient, pre-safety)")
        print(f"  recommended alloc    : {r['recommended_percent']}%  "
              f"(~{units} {label})  [safety-floored]")
        if sla:
            print(f"  SLA breaches         : {sla['breaches']}/{sla['samples']} "
                  f"({sla['breach_rate']}% of samples exceeded allocation)")

    print("\n---------------------- COST COMPARISON ----------------------")
    print(f"  Static (100% provisioned) : ${result['static_cost']['total']}/month")
    print(f"  Predictive allocation     : ${result['predictive_cost']['total']}/month")
    print(f"  MONTHLY SAVINGS           : ${result['monthly_savings']}  "
          f"({result['savings_percent']}% reduction)")
    print("=================================================================")


if __name__ == "__main__":
    print_report()
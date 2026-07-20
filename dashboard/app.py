"""
Live resource-monitoring dashboard.

Auto-refreshes every few seconds: re-reads the metrics table, re-runs the
forecast + recommender, and redraws. Fire the load generator in a terminal
and watch the charts + recommendations react in real time.

Run from project root with:
    streamlit run dashboard/app.py
"""

import os
import sys
import numpy as np
import pandas as pd
import streamlit as st

# --- make project root importable when launched via `streamlit run` ---
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from model.features import load_dataframe, TARGETS
from model.horizon import horizon_summary
from service.recommender import build_recommendation, sla_check, RESOURCE_MAP

# auto-refresh (graceful fallback if package missing)
try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False


st.set_page_config(page_title="Predictive Resource Monitor", layout="wide")

# ---------------- Sidebar controls ----------------
st.sidebar.title("Controls")
refresh_secs = st.sidebar.slider("Refresh interval (seconds)", 2, 30, 5)
window = st.sidebar.slider("Chart window (samples)", 30, 500, 150)
live = st.sidebar.toggle("Live updating", value=True)

if live and HAS_AUTOREFRESH:
    st_autorefresh(interval=refresh_secs * 1000, key="datarefresh")
elif live and not HAS_AUTOREFRESH:
    st.sidebar.warning("Install streamlit-autorefresh for live mode.")

# ---------------- Load data ----------------
df = load_dataframe()

st.title("Predictive Resource Monitoring System")

if df.empty:
    st.warning("No metrics yet. Run the collector to start logging data.")
    st.stop()

df_view = df.tail(window).copy()
latest = df.iloc[-1]

# ---------------- Top KPI row ----------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("CPU now", f"{latest['cpu_percent']:.1f}%")
c2.metric("Memory now", f"{latest['mem_percent']:.1f}%")
c3.metric("Disk now", f"{latest['disk_percent']:.1f}%")
c4.metric("Samples logged", f"{len(df)}")

# ---------------- Live charts with forecast overlay ----------------
st.subheader("Live Utilization + Forecast")

RES_LABELS = {
    "cpu_percent": "CPU %",
    "mem_percent": "Memory %",
    "disk_percent": "Disk %",
}

chart_cols = st.columns(3)
for col, tgt in zip(chart_cols, TARGETS):
    with col:
        st.caption(RES_LABELS[tgt])

        # historical window
        hist = df_view[["ts", tgt]].rename(columns={tgt: "actual"}).set_index("ts")

        # forecast trajectory appended after the last timestamp
        summary = horizon_summary(tgt)
        if summary:
            preds = summary["all_predictions"]
            interval = (df["ts"].iloc[-1] - df["ts"].iloc[-2]) if len(df) >= 2 else pd.Timedelta(seconds=3)
            future_ts = [df["ts"].iloc[-1] + interval * (i + 1) for i in range(len(preds))]
            fc = pd.DataFrame({"forecast": preds}, index=pd.DatetimeIndex(future_ts))
            combined = pd.concat([hist, fc], axis=0)
        else:
            combined = hist

        st.line_chart(combined, height=220)

# ---------------- Recommendation + cost ----------------
st.subheader("Allocation Recommendation")

result = build_recommendation()
rows = []
for tgt, r in result["recommendations"].items():
    label, total = RESOURCE_MAP[tgt]
    units = round(r["recommended_percent"] / 100 * total, 2)
    sla = sla_check(tgt, r["recommended_percent"])
    rows.append({
        "Resource": RES_LABELS[tgt],
        "Forecast P95 (60s)": f"{r['forecast_p95']}%",
        "Forecast-only": f"{r['forecast_alloc']}%",
        "Recommended": f"{r['recommended_percent']}%",
        "Provisioned": f"{units} {label}",
        "SLA breaches": f"{sla['breach_rate']}%" if sla else "n/a",
    })

st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ---------------- Cost comparison ----------------
st.subheader("Cost Impact (AWS on-demand, monthly)")
m1, m2, m3 = st.columns(3)
m1.metric("Static (100% provisioned)", f"${result['static_cost']['total']}")
m2.metric("Predictive allocation", f"${result['predictive_cost']['total']}")
m3.metric(
    "Monthly savings",
    f"${result['monthly_savings']}",
    f"{result['savings_percent']}% reduction",
)

st.caption(
    "Forecast-driven allocation floored by a 5% max SLA-breach constraint. "
    "Prices: AWS Fargate on-demand (compute/memory) + EBS gp3 (storage), us-east-1."
)
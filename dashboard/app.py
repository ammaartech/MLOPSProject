"""
Dashboard — the twelve-stage pipeline, visible.

    streamlit run dashboard/app.py

Tabs, all reading from the database rather than recomputing. A customer
account sees the first four; an admin sees all nine.

    Overview        champions, live utilisation, allocation and cost
    Forecast Studio the forecast, interactively
    Capacity        headroom and what the allocation would be
    Digital Twin    replay a what-if against the recorded data
    ------------------------------------------------- admin only below
    Data Health     quality gate, collection gaps, segments, cleaning audit
    Model           baseline ladder, registry, promotion decisions, drift
    Cost & SLA      walk-forward backtest and the cost/breach tradeoff curve
    Lineage         trace any model to its data; browse and EDIT config
    Logs            every recorded event, six tables in one stream

Everything on screen is a stored artifact of a pipeline run, so no two
panels can disagree with each other. The one exception is the config
editor, which writes — and that is deliberate: changing headroom here and
watching the cost move is the clearest demonstration that no value in
this system is hardcoded.
"""

import os
import sys

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config                                              # noqa: E402
from crud.query import execute_query                        # noqa: E402
from dashboard.auth import require_login, logout            # noqa: E402

st.set_page_config(page_title="Predictive Resource Monitor", layout="wide")

session = require_login()
role = session["role"]


# ----------------------------------------------------------------------
# Cached readers — the dashboard reads, the pipeline writes
# ----------------------------------------------------------------------
@st.cache_data(ttl=5)
def clean_frame(run_id=None):
    from pipeline.etl import read_clean

    return read_clean(run_id)


@st.cache_data(ttl=5)
def latest_run():
    from pipeline.etl import latest_run as _latest

    return _latest()


@st.cache_data(ttl=10)
def run_history(limit=20):
    from pipeline.etl import run_history as _history

    return pd.DataFrame(_history(limit), columns=[
        "run_id", "started", "finished", "source", "rows_in", "rows_out",
        "gate", "status", "data_fp", "config_fp",
    ])


@st.cache_data(ttl=10)
def quality_checks(run_id):
    rows = execute_query(
        "SELECT check_name, status, value, detail FROM quality_checks "
        "WHERE run_id = ? ORDER BY id", (run_id,), fetch=True,
    ) or []
    return pd.DataFrame(rows, columns=["check", "status", "value", "detail"])


@st.cache_data(ttl=10)
def registry():
    from tracking.mlflow_tracker import registry as _registry

    return pd.DataFrame(_registry(), columns=[
        "model_id", "target", "algorithm", "mae", "baseline_mae",
        "improvement_pct", "is_champion", "feature_version",
        "rejected_reason", "created_at",
    ])


@st.cache_data(ttl=10)
def champions():
    from tracking.lineage import champions as _champions

    return _champions()


@st.cache_data(ttl=30)
def backtest(_frame):
    from evaluation.backtest import combined, run_all

    results = run_all(_frame)
    return results, combined(results)


@st.cache_data(ttl=30)
def ladder(_frame, target):
    from model.baseline import run_ladder
    from model.features import build_features, chronological_split
    import numpy as np

    X, y, meta = build_features(_frame, target)
    if X.empty:
        return pd.DataFrame()
    split = chronological_split(X, y, meta)
    index = np.arange(len(X))[split["split_index"]:]
    table, _ = run_ladder(split, target, series=meta["actual_now"],
                          test_index=index)
    return table


@st.cache_data(ttl=5)
def latest_process_alerts():
    rows = execute_query(
        "SELECT ts, breach_rate, process_payload FROM recommendations "
        "WHERE type = 'process_alert' ORDER BY id DESC LIMIT 5",
        fetch=True
    )
    return rows or []


# The system writes no log FILE. Every event it has ever produced is a
# row in one of six tables, which is the stronger arrangement — a log
# line can disagree with the database, a row cannot — but it means "what
# happened last night?" is six queries in six shapes. This flattens them
# into one stream: when, from where, how bad, what, and the detail.
#
# It reads only tables the pipeline already writes. Nothing here adds a
# logging side effect, so the log cannot drift from the artifacts.
#
# The wrapping SELECT is not decoration. Two timestamp formats are in
# use — `2026-07-29T20:08:09` from the pipeline and `2026-07-29 07:16:54`
# from config_history — and 'T' sorts above ' ', so a plain string sort
# interleaves same-day events wrongly. Normalising the separator makes
# the sort chronological. It has to happen in a subquery because SQLite
# only accepts bare column names in the ORDER BY of a compound SELECT.
EVENT_LOG_SQL = """
SELECT REPLACE(at, 'T', ' ') AS at, source, level, event, detail FROM (

SELECT started_at AS at,
       'etl' AS source,
       CASE WHEN status = 'FAILED'     THEN 'ERROR'
            WHEN gate_verdict = 'FAIL' THEN 'ERROR'
            WHEN gate_verdict = 'WARN' THEN 'WARN'
            ELSE 'INFO' END AS level,
       'ETL run ' || run_id || ' ' || COALESCE(status, 'running') AS event,
       COALESCE(error, gate_detail,
                COALESCE(source_kind, '?') || ': ' ||
                COALESCE(rows_in, 0) || ' in / ' ||
                COALESCE(rows_out, 0) || ' out') AS detail
FROM pipeline_runs

UNION ALL
-- PASS results are excluded: 13 checks a run would bury everything else,
-- and a check that passed is not an event.
SELECT checked_at, 'quality',
       CASE WHEN status = 'FAIL' THEN 'ERROR' ELSE 'WARN' END,
       'run ' || run_id || ' — ' || check_name || ' ' || status,
       COALESCE(detail, '')
FROM quality_checks
WHERE status <> 'PASS'

UNION ALL
SELECT created_at, 'model',
       CASE WHEN is_champion = 1 THEN 'INFO' ELSE 'WARN' END,
       target || ' ' || COALESCE(algorithm, '?') || ' ' ||
           CASE WHEN is_champion = 1 THEN 'PROMOTED' ELSE 'REJECTED' END,
       COALESCE(rejected_reason,
                'MAE ' || COALESCE(ROUND(mae, 4), '?') ||
                ' vs baseline ' || COALESCE(ROUND(baseline_mae, 4), '?'))
FROM model_versions

UNION ALL
SELECT detected_at, 'drift', 'WARN',
       target || ' drift — ' || COALESCE(action, '?'),
       COALESCE(outcome || '. ', '') || COALESCE(detail, '')
FROM drift_events

UNION ALL
SELECT changed_at, 'config', 'INFO',
       'config ' || key,
       COALESCE(old_value, '(unset)') || ' -> ' || new_value ||
           '  [' || COALESCE(source, '?') || ']'
FROM config_history

UNION ALL
SELECT ts, 'alert', 'WARN', 'process alert',
       SUBSTR(COALESCE(process_payload, ''), 1, 400)
FROM recommendations
WHERE type = 'process_alert'

)
ORDER BY at DESC
LIMIT ?
"""


@st.cache_data(ttl=10)
def event_log(limit=500):
    rows = execute_query(EVENT_LOG_SQL, (limit,), fetch=True) or []
    return pd.DataFrame(rows, columns=["at", "source", "level", "event", "detail"])


def targets():
    return config.get_json("features.targets")


# ----------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------
st.sidebar.title("Predictive Resource Monitor")
st.sidebar.caption(f"**{session['email']}** ({role})")
if st.sidebar.button("Log out"):
    logout()

# ----------------------------------------------------------------------
# Which environment am I looking at?
#
# The Kubernetes overlays serve three identical-looking dashboards on
# three ports, from three separate databases. With nothing on screen to
# tell them apart, "did that config edit reach production?" can only be
# answered from a terminal — so the isolation the namespaces provide is
# real but unverifiable, which is nearly as bad as not having it.
#
# RMS_ENVIRONMENT is set by each overlay and by nothing else: when it is
# absent this is a local `streamlit run`, and saying so is the point.
# HOSTNAME is the pod, which with replicas > 1 is the only way to see
# which one answered — and therefore that session affinity is holding.
# Both are display-only. Every value that changes behaviour still comes
# from the config table of the database this pod is attached to.
# ----------------------------------------------------------------------
_environment = os.environ.get("RMS_ENVIRONMENT")
if _environment:
    _colour = {"production": "red", "qs": "orange", "dev": "blue"}
    st.sidebar.markdown(
        f":{_colour.get(_environment, 'grey')}-background"
        f"[**{_environment.upper()}**]"
    )
    _pod = os.environ.get("HOSTNAME")
    if _pod:
        st.sidebar.caption(f"pod `{_pod}`")
else:
    st.sidebar.caption("environment **local**")

run = latest_run()
if run is None:
    st.error("No successful ETL run. Run `python -m orchestration.run_pipeline` first.")
    st.stop()

st.sidebar.caption(f"ETL run **{run['run_id']}** — {run['started_at']}")
st.sidebar.caption(f"data `{run['data_fingerprint']}`")
st.sidebar.caption(f"config `{config.fingerprint()}`")
st.sidebar.caption(f"gate **{run['gate_verdict']}**")

from streamlit_autorefresh import st_autorefresh

window = st.sidebar.slider("Chart window (samples)", 50, 800, 300, step=50)

st.sidebar.markdown("---")
auto_refresh = st.sidebar.toggle("Auto-refresh live data", value=True)
if auto_refresh:
    st_autorefresh(interval=10000, key="data_refresh")
    st.cache_data.clear()
    config.invalidate()
elif st.sidebar.button("Refresh data manually"):
    st.cache_data.clear()
    config.invalidate()
    st.rerun()

frame = clean_frame(run["run_id"])
if frame.empty:
    st.error("The latest run produced no cleaned rows.")
    st.stop()

# Which tabs exist depends on who is logged in. The customer surface is
# the subset an admin also needs — you cannot judge whether an allocation
# is sane without seeing the allocation — so admin is customer plus the
# operational tabs, not a disjoint set. Unused tab variables stay None so
# the `if tab_X is not None:` guard around each section below skips it.
CUSTOMER_TABS = ["Overview", "🔮 Forecast Studio", "Capacity", "🧪 Digital Twin"]
ADMIN_TABS = ["Data Health", "Model", "Cost & SLA", "Lineage & Config", "🧾 Logs"]

tab_overview = tab_forecast = tab_capacity = tab_twin = None
tab_health = tab_model = tab_cost = tab_lineage = tab_logs = None

if role == "admin":
    (tab_overview, tab_forecast, tab_capacity, tab_twin,
     tab_health, tab_model, tab_cost, tab_lineage,
     tab_logs) = st.tabs(CUSTOMER_TABS + ADMIN_TABS)
else:
    tab_overview, tab_forecast, tab_capacity, tab_twin = st.tabs(CUSTOMER_TABS)


# ----------------------------------------------------------------------
# Overview
# ----------------------------------------------------------------------
if tab_overview is not None:
    with tab_overview:
        st.header("Predictive Resource Allocations & System State")

        from service.recommender import build_recommendation

        recommendation = build_recommendation(frame, persist=False)

        # ------------------------------------------------------------------
        # Prominent Recommended Allocations Header & Cards
        # ------------------------------------------------------------------
        st.subheader("🎯 Live Recommended Resource Allocations")
        st.caption("Forecast-driven allocations adjusted with SLA safety floors to minimize infrastructure spend.")

        rec_cols = st.columns(3)
        res_labels = {
            "cpu_percent": ("💻 CPU Allocation", "vCPUs"),
            "mem_percent": ("🧠 Memory Allocation", "GB RAM"),
            "disk_read_mb_s": ("💾 Storage I/O Allocation", "MB/s I/O"),
        }

        summary_rows = []
        for idx, (target, r) in enumerate(recommendation["recommendations"].items()):
            icon_title, unit_lbl = res_labels.get(target, (target, r["unit_label"]))
            with rec_cols[idx % 3]:
                st.metric(
                    label=icon_title,
                    value=f"{r['recommended_percent']}%",
                    delta=f"{r['units']} {r['unit_label']}",
                    delta_color="off",
                )
                st.caption(f"**Model**: `{r['predictor']}`")
                st.caption(f"**Forecast**: Peak `{r['forecast_peak']}%` | P95 `{r['forecast_p95']}%`")
                st.caption(f"**Monthly Cost**: **${r['monthly_cost']}** *(Static: ${r['static_cost']})*")
                if r["floored"]:
                    st.caption(f"🛡️ *Safety Floor*: {r['floor_reason']}")
                else:
                    st.caption("✅ *SLA Status*: Compliant with forecast allocation")

            summary_rows.append({
                "Resource": target,
                "Recommended Alloc (%)": f"{r['recommended_percent']}%",
                "Allocated Units": f"{r['units']} {r['unit_label']}",
                "Forecast Peak": f"{r['forecast_peak']}%",
                "Predictor": r["predictor"],
                "Monthly Cost": f"${r['monthly_cost']}",
                "Static Cost": f"${r['static_cost']}",
                "SLA Safety Floor": r["floor_reason"] if r["floored"] else "Compliant",
            })

        st.dataframe(pd.DataFrame(summary_rows), width="stretch", hide_index=True)

        st.markdown("---")

        st.subheader("Financial Impact & System Metrics")
        columns = st.columns(4)
        columns[0].metric("Static (100%)", f"${recommendation['static_cost']['total']}/mo")
        columns[1].metric("Predictive Allocation", f"${recommendation['predictive_cost']['total']}/mo")
        columns[2].metric("Snapshot Saving", f"${recommendation['monthly_savings']}", f"{recommendation['savings_percent']}%")
        columns[3].metric("Data Samples", f"{len(frame)}", f"{frame['segment_id'].nunique()} segments")

        st.info(
            "The snapshot saving is a single instant. The measured figure is in "
            "**Cost & SLA**, which replays every allocation decision — on this "
            "data the two disagree, and the replay is the one to trust."
        )

        # Process alerts warning
        alerts = latest_process_alerts()
        if alerts:
            latest_alert = alerts[0]
            st.warning(f"⚠️ **Memory Headroom Warning** (Logged at {latest_alert[0]})")
            st.markdown(f"Live memory usage crossed alert threshold at **{latest_alert[1]:.1f}%**.")
        
            # Display top 5 process list
            import json
            try:
                procs = json.loads(latest_alert[2])
                proc_df = pd.DataFrame(procs)
                # rename columns for nice display
                proc_df.columns = ["PID", "Process Name", "Memory (MB)"]
                st.dataframe(proc_df, hide_index=True)
            except Exception as e:
                st.caption(f"Could not parse process list: {e}")

        st.subheader("Champions serving production")
        st.dataframe(champions(), width="stretch", hide_index=True)

        st.subheader("Utilisation")
        recent = frame.tail(window)
        st.line_chart(recent.set_index("ts")[targets()])

        st.subheader("Forecast Trajectory & Allocation vs Demand")
        from serving.predictor import forecast_horizon

        for target, r in recommendation["recommendations"].items():
            left, right = st.columns([3, 2])
            with left:
                trajectory, meta = forecast_horizon(target, df=frame)
                actual_df = recent.copy()
                actual_df["ts"] = pd.to_datetime(actual_df["ts"])
                actual_df = actual_df.set_index("ts")[[target]].rename(columns={target: "actual"})
                actual_df["allocated"] = r["recommended_percent"]

                if not trajectory.empty:
                    traj_df = trajectory.copy()
                    traj_df["ts"] = pd.to_datetime(traj_df["ts"])
                    traj_df = traj_df.set_index("ts")[["predicted"]].rename(columns={"predicted": "forecast"})
                    last_ts = actual_df.index[-1]
                    actual_df.loc[last_ts, "forecast"] = actual_df["actual"].iloc[-1]
                    chart = pd.concat([actual_df, traj_df], axis=0)
                else:
                    chart = actual_df

                st.caption(f"**{target}** — {r['predictor']} | Peak: {r['forecast_peak']}% | P95: {r['forecast_p95']}%")
                st.line_chart(chart)
            with right:
                st.metric(f"{target} allocation",
                          f"{r['recommended_percent']}%",
                          f"{r['units']} {r['unit_label']}")
                st.caption(f"🔮 **Forecast Trajectory**: peak {r['forecast_peak']}% | p95 {r['forecast_p95']}%")
                st.caption(f"Forecast wanted {r['forecast_alloc']}%")
                if r["floored"]:
                    st.caption(f"🛡️ Safety floor: {r['floor_reason']}")
                st.caption(f"Breaches {r['breach_rate']}% "
                           f"in {r['breach_episodes']} episode(s)")


# ----------------------------------------------------------------------
# Forecast Studio
# ----------------------------------------------------------------------
if tab_forecast is not None:
    with tab_forecast:
        st.header("🔮 Forecast Studio & Multi-Step Predictor")
        st.caption(
            "Interactive multi-step horizon forecasting engine with expanding uncertainty bands, "
            "model comparison, and live traffic scenario simulation."
        )

        col_ctrl1, col_ctrl2, col_ctrl3 = st.columns([2, 2, 2])
        with col_ctrl1:
            fc_target = st.selectbox("Forecast Target", targets(), key="fc_studio_target")
        with col_ctrl2:
            fc_steps = st.slider("Forecast Horizon (steps)", 10, 100, 30, step=5, key="fc_studio_steps")
        with col_ctrl3:
            show_bounds = st.checkbox("Show Uncertainty Band (95% CI)", value=True, key="fc_studio_bounds")

        from serving.predictor import forecast_horizon, resolve_champion
        from service.recommender import recommend_percent

        trajectory, meta = forecast_horizon(fc_target, steps=fc_steps, df=frame)
        r = recommend_percent(fc_target, df=frame)

        if not trajectory.empty:
            cadence = config.get_int("pipeline.nominal_cadence_sec")
            horizon_sec = len(trajectory) * cadence

            # Metric Summary Bar
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Projected Peak", f"{meta['forecast_peak']}%")
            m2.metric("Projected P95", f"{meta['forecast_p95']}%")
            m3.metric("Forecast Window", f"{horizon_sec}s ({len(trajectory)} steps)")
            m4.metric("Champion Model", meta["model_id"].split("-")[0], f"MAE {meta.get('mae', 0.0):.3f}")

            # Main Forecast Trajectory Chart
            recent_fc = frame.tail(window).copy()
            recent_fc["ts"] = pd.to_datetime(recent_fc["ts"])
            actual_chart = recent_fc.set_index("ts")[[fc_target]].rename(columns={fc_target: "Actual Demand"})

            traj_chart = trajectory.copy()
            traj_chart["ts"] = pd.to_datetime(traj_chart["ts"])

            if show_bounds and "upper_bound" in traj_chart.columns:
                traj_chart = traj_chart.set_index("ts")[["predicted", "upper_bound", "lower_bound"]].rename(
                    columns={"predicted": "Model Forecast", "upper_bound": "Upper 95% Bound", "lower_bound": "Lower 95% Bound"}
                )
            else:
                traj_chart = traj_chart.set_index("ts")[["predicted"]].rename(columns={"predicted": "Model Forecast"})

            # Bridge point
            last_ts = actual_chart.index[-1]
            last_val = actual_chart["Actual Demand"].iloc[-1]
            actual_chart.loc[last_ts, "Model Forecast"] = last_val
            if show_bounds:
                actual_chart.loc[last_ts, "Upper 95% Bound"] = last_val
                actual_chart.loc[last_ts, "Lower 95% Bound"] = last_val

            if r:
                actual_chart["Recommended Allocation"] = r["recommended_percent"]

            combined_fc_chart = pd.concat([actual_chart, traj_chart], axis=0)

            st.subheader(f"Multi-Step Forecast Trajectory — {fc_target} ({horizon_sec}s Horizon)")
            st.line_chart(combined_fc_chart)

            # Details and Baseline Comparison
            exp_left, exp_right = st.columns(2)
            with exp_left:
                st.subheader("Forecast Metadata & Champion Lineage")
                st.markdown(f"""
                - **Predictor Type**: `{meta.get('predictor', 'N/A')}`
                - **Active Champion ID**: `{meta.get('model_id', 'N/A')}`
                - **Nominal Cadence**: `{cadence} seconds/sample`
                - **Forecast Horizon**: `{horizon_sec} seconds` (`{fc_steps}` iterative steps)
                - **Expected Error (MAE)**: `{meta.get('mae', 'N/A')}`
                """)
            with exp_right:
                st.subheader("Recommended Allocation Impact")
                if r:
                    st.markdown(f"""
                    - **Forecast-Driven Demand**: `{r['forecast_alloc']}%`
                    - **Final Recommended Allocation**: **`{r['recommended_percent']}%`** (`{r['units']} {r['unit_label']}`)
                    - **SLA Safety Floor Adjustment**: `{r['floor_reason']}`
                    - **Estimated Monthly Spend**: `${r['monthly_cost']}` *(vs static ${r['static_cost']})*
                    """)

            st.divider()

            # Stress Test / Traffic Spike Simulation
            st.subheader("⚡ Live Traffic Spike Forecast Simulation")
            st.caption("Simulate a sudden workload burst on current features and watch how the forecaster adapts.")
            sim_spike = st.slider("Simulated Workload Spike Factor", 1.0, 3.0, 1.5, step=0.1, key="sim_spike_slider")

            if st.button("🔥 Run Forecast Stress Test"):
                spiked_frame = frame.copy()
                spiked_frame[fc_target] = spiked_frame[fc_target] * sim_spike
                spiked_traj, spiked_meta = forecast_horizon(fc_target, steps=fc_steps, df=spiked_frame)
                if not spiked_traj.empty:
                    st.success(f"Spike forecast generated! Projected Peak: **{spiked_meta['forecast_peak']}%** (Original: {meta['forecast_peak']}%)")
                    spiked_chart = spiked_traj.set_index("step")[["predicted"]].rename(columns={"predicted": f"Spiked Forecast ({sim_spike}x)"})
                    orig_chart = trajectory.set_index("step")[["predicted"]].rename(columns={"predicted": "Normal Forecast (1.0x)"})
                    st.line_chart(pd.concat([orig_chart, spiked_chart], axis=1))
        else:
            st.warning(f"Could not generate forecast for {fc_target}. Check data health or model registry.")


# ----------------------------------------------------------------------
# Capacity
# ----------------------------------------------------------------------
if tab_capacity is not None:
    with tab_capacity:
        st.header("Capacity Monitor")
        st.caption(
            "Allocation and forecast peak utilization visualised as 3D cylinders "
            "against reference node capacities. If the forecast crosses the alert threshold, "
            "a recommended expansion capacity is shown."
        )
    
        # Read threshold from config
        threshold_val = config.get_float("policy.capacity_alert_threshold", 80.0)
    
        def get_current_usage(target, fallback_df):
            """Get the absolute most recent raw metric from the collector."""
            try:
                from database.connection import get_connection
                import pandas as pd
                con = get_connection()
                if con:
                    # The collector writes live data here every few seconds
                    df = pd.read_sql_query("SELECT * FROM metrics ORDER BY ts DESC LIMIT 1", con)
                    con.close()
                    if not df.empty and target in df.columns:
                        return float(df[target].iloc[0])
            except Exception:
                pass
            
            # Fallback to the latest frame row if the live query fails
            if not fallback_df.empty and target in fallback_df.columns:
                return float(fallback_df[target].iloc[-1])
            return 0.0

        from service.recommender import build_recommendation
        recs = build_recommendation(frame, persist=False)
    
        # CSS Styles for Cylinders
        st.markdown("""
        <style>
        .cylinder-container {
            display: flex;
            justify-content: space-around;
            align-items: flex-end;
            background: rgba(255, 255, 255, 0.02);
            border-radius: 12px;
            padding: 30px 10px;
            border: 1px solid rgba(255, 255, 255, 0.05);
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
            margin-bottom: 25px;
        }
        .cylinder-column {
            display: flex;
            flex-direction: column;
            align-items: center;
            width: 30%;
        }
        .cylinder-flex {
            display: flex;
            align-items: flex-end;
            justify-content: center;
            height: 320px;
            position: relative;
        }
        .cylinder-outer {
            width: 70px;
            height: 280px;
            background: rgba(255, 255, 255, 0.05);
            border: 2px solid rgba(255, 255, 255, 0.15);
            border-radius: 35px / 15px;
            position: relative;
            box-shadow: inset 0 0 15px rgba(0,0,0,0.6);
            margin-bottom: 5px;
        }
        .cylinder-outer::before {
            content: '';
            position: absolute;
            top: -8px;
            left: -2px;
            width: 70px;
            height: 16px;
            background: rgba(255, 255, 255, 0.08);
            border: 2px solid rgba(255, 255, 255, 0.2);
            border-radius: 50%;
            z-index: 5;
        }
        .cylinder-fill {
            position: absolute;
            bottom: 0;
            left: 0;
            width: 100%;
            border-radius: 0 0 35px 35px / 0 0 15px 15px;
            background: linear-gradient(180deg, rgba(108, 92, 231, 0.8) 0%, rgba(162, 155, 254, 0.6) 100%);
            box-shadow: 0 0 10px rgba(108, 92, 231, 0.3);
            transition: height 0.5s ease-in-out;
        }
        .cylinder-fill-top {
            position: absolute;
            top: -8px;
            left: 0;
            width: 100%;
            height: 16px;
            border-radius: 50%;
            box-shadow: inset 0 0 4px rgba(255,255,255,0.4);
        }
        .threshold-line {
            position: absolute;
            left: 0;
            width: 100%;
            height: 2px;
            background: rgba(255, 118, 117, 0.8);
            z-index: 6;
            box-shadow: 0 0 5px rgba(255, 118, 117, 0.6);
        }
        .threshold-line::after {
            content: 'Alert';
            position: absolute;
            right: -32px;
            top: -7px;
            color: #ff7675;
            font-size: 9px;
            font-weight: bold;
        }
        .forecast-marker {
            position: absolute;
            left: -8px;
            width: 82px;
            height: 2px;
            background: #fdcb6e;
            z-index: 7;
            box-shadow: 0 0 8px #fdcb6e;
        }
        .forecast-marker::after {
            content: '▲';
            position: absolute;
            left: -10px;
            top: -7px;
            color: #fdcb6e;
            font-size: 10px;
        }
        .cylinder-label {
            font-weight: bold;
            color: #dfe6e9;
            font-size: 13px;
            margin-top: 10px;
        }
        .cylinder-sublabel {
            color: #b2bec3;
            font-size: 11px;
        }
        /* Translucent cylinder for recommended addition */
        .cylinder-addition {
            width: 70px;
            height: 280px;
            background: rgba(0, 184, 148, 0.03);
            border: 2px dashed rgba(0, 184, 148, 0.3);
            border-radius: 35px / 15px;
            position: relative;
            margin-left: 12px;
            box-shadow: 0 0 10px rgba(0, 184, 148, 0.1);
            margin-bottom: 5px;
        }
        .cylinder-addition::before {
            content: '';
            position: absolute;
            top: -8px;
            left: -2px;
            width: 70px;
            height: 16px;
            background: rgba(0, 184, 148, 0.05);
            border: 2px dashed rgba(0, 184, 148, 0.4);
            border-radius: 50%;
            z-index: 5;
        }
        .cylinder-addition-fill {
            position: absolute;
            bottom: 0;
            left: 0;
            width: 100%;
            border-radius: 0 0 35px 35px / 0 0 15px 15px;
            background: rgba(0, 184, 148, 0.15);
            transition: height 0.5s ease-in-out;
        }
        .cylinder-addition-fill::before {
            content: '';
            position: absolute;
            top: -8px;
            left: 0;
            width: 100%;
            height: 16px;
            background: rgba(0, 184, 148, 0.25);
            border-radius: 50%;
        }
        .cylinder-addition-text {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%) rotate(-90deg);
            color: rgba(0, 184, 148, 0.7);
            font-size: 8px;
            font-weight: bold;
            white-space: nowrap;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        </style>
        """, unsafe_allow_html=True)

        # Let's map target name to display details and capacity config key
        resource_info = {
            "cpu_percent": {
                "name": "CPU Usage",
                "capacity_key": "node.vcpus",
                "unit": "vCPUs",
                "fill_color": "linear-gradient(180deg, rgba(9, 132, 227, 0.8) 0%, rgba(116, 185, 255, 0.6) 100%)",
                "top_color": "rgba(116, 185, 255, 0.8)"
            },
            "mem_percent": {
                "name": "Memory Usage",
                "capacity_key": "node.ram_gb",
                "unit": "GB RAM",
                "fill_color": "linear-gradient(180deg, rgba(108, 92, 231, 0.8) 0%, rgba(162, 155, 254, 0.6) 100%)",
                "top_color": "rgba(162, 155, 254, 0.8)"
            },
            "disk_read_mb_s": {
                "name": "Disk I/O Usage",
                "capacity_key": "node.storage_gb",  # Sized against storage capacity config
                "unit": "GB Storage",
                "fill_color": "linear-gradient(180deg, rgba(225, 112, 85, 0.8) 0%, rgba(250, 177, 160, 0.6) 100%)",
                "top_color": "rgba(250, 177, 160, 0.8)"
            }
        }
    
        html_cols = []

        for target, info in resource_info.items():
            if target not in recs["recommendations"]:
                continue

            r = recs["recommendations"][target]
            capacity = config.get_int(info["capacity_key"])
            current_usage_pct = get_current_usage(target, frame)

            forecast_pct = r["forecast_peak"]

            # Pixel heights (out of 280px total)
            fill_px      = int(min(1.0, max(0.0, current_usage_pct / 100.0)) * 280)
            threshold_px = int(min(1.0, max(0.0, threshold_val   / 100.0)) * 280)
            forecast_px  = int(min(1.0, max(0.0, forecast_pct    / 100.0)) * 280)

            # Inline the per-resource colour directly — no mid-loop st.markdown
            fill_style = (
                f"height:{fill_px}px;"
                f"background:{info['fill_color']};"
                f"box-shadow:0 0 10px rgba(108,92,231,0.2);"
            )
            fill_top_style = f"background:{info['top_color']};"

            # Recommended addition cylinder (only when forecast > threshold)
            addition_html = ""
            if forecast_pct > threshold_val:
                rec_pct   = r["recommended_percent"]
                rec_units = r["units"]
                addition_px = int(min(1.0, max(0.0, rec_pct / 100.0)) * 280)
                addition_html = (
                    f'<div class="cylinder-addition">'
                    f'<div class="cylinder-addition-fill" style="height:{addition_px}px;"></div>'
                    f'<div class="cylinder-addition-text">ADDITION: {rec_units:.1f} {info["unit"]}</div>'
                    f'</div>'
                )

            html_cols.append(
                f'<div class="cylinder-column">'
                f'  <div class="cylinder-flex">'
                f'    <div class="cylinder-outer">'
                f'      <div class="cylinder-fill" style="{fill_style}">'
                f'        <div class="cylinder-fill-top" style="{fill_top_style}"></div>'
                f'      </div>'
                f'      <div class="threshold-line" style="bottom:{threshold_px}px;"></div>'
                f'      <div class="forecast-marker"  style="bottom:{forecast_px}px;"></div>'
                f'    </div>'
                f'    {addition_html}'
                f'  </div>'
                f'  <div class="cylinder-label">{info["name"]}</div>'
                f'  <div class="cylinder-sublabel">Capacity: {capacity} {info["unit"]}</div>'
                f'</div>'
            )
        
        # Render all cylinders in a flex container
        st.markdown(f'<div class="cylinder-container">{"".join(html_cols)}</div>', unsafe_allow_html=True)
    
        # Legend & Metrics
        left, right = st.columns(2)
        with left:
            st.subheader("Capacity Legend")
            st.markdown(f"""
            - <span style="color: #6c5ce7; font-weight: bold;">Cylinder Fluid</span>: Current active utilization level (from online features)
            - <span style="color: #ff7675; font-weight: bold;">Red Horizontal Line (Alert)</span>: Safety headroom warning threshold (**{threshold_val}%**)
            - <span style="color: #fdcb6e; font-weight: bold;">Yellow Pointer (▲)</span>: Near-term forecasted peak demand
            - <span style="color: #00b894; font-weight: bold;">Dashed Outlined Cylinder</span>: Recommended addition capacity to clear threshold
            """, unsafe_allow_html=True)
        
        with right:
            st.subheader("Current Allocations")
            for target, info in resource_info.items():
                if target not in recs["recommendations"]:
                    continue
                r = recs["recommendations"][target]
                capacity = config.get_int(info["capacity_key"])
                current_pct = get_current_usage(target, frame)
            
                st.markdown(
                    f"**{info['name']}**"
                    f"\n* Current Usage: {current_pct:.1f}% ({ (current_pct/100.0)*capacity:.2f} / {capacity} {info['unit']})"
                    f"\n* Forecasted Peak: {r['forecast_peak']:.1f}% ({ (r['forecast_peak']/100.0)*capacity:.2f} {info['unit']})"
                    f"\n* Recommended Allocation: **{r['recommended_percent']}%** ({r['units']:.2f} {info['unit']})"
                )
                st.divider()


# ----------------------------------------------------------------------
# Digital Twin
# ----------------------------------------------------------------------
if tab_twin is not None:
    with tab_twin:
        st.header("🧪 Digital Twin Simulator")
        st.caption(
            "Run the full 12-stage pipeline against a synthetic load scenario "
            "in an isolated database, then compare all policies side-by-side."
        )

        SCENARIOS = [
            "gap_injection",
            "regime_change",
            "sustained_spike",
            "cadence_drift",
            "multi_host_shift",
        ]

        col_sel, col_btn = st.columns([3, 1])
        with col_sel:
            scenario = st.selectbox(
                "Scenario",
                SCENARIOS,
                help="gap_injection: drops 20 min of samples | "
                     "regime_change: idle→high step | "
                     "sustained_spike: gradual ramp to 90%+ | "
                     "cadence_drift: sampling interval varies | "
                     "multi_host_shift: shifted baseline",
            )
        with col_btn:
            st.write("")
            st.write("")
            run_twin_btn = st.button("▶ Run Twin", type="primary", width="stretch")

        twin_db = f"data/metrics_twin_{scenario}.db"

        if run_twin_btn:
            import subprocess, sys as _sys
            gen_cmd = [
                _sys.executable, "-m", "collector.scenario_generator",
                "--scenario", scenario,
                "--db", twin_db,
            ]
            run_cmd = [
                _sys.executable, "-m", "orchestration.run_twin",
                "--scenario", scenario,
                "--db", twin_db,
            ]
            with st.status(f"Running twin for **{scenario}**…", expanded=True) as status:
                st.write("Generating synthetic data…")
                r1 = subprocess.run(gen_cmd, capture_output=True, text=True)
                if r1.returncode != 0:
                    st.error(f"Scenario generator failed:\n```\n{r1.stderr}\n```")
                    status.update(label="Failed", state="error")
                else:
                    st.write("Running 12-stage pipeline…")
                    r2 = subprocess.run(run_cmd, capture_output=True, text=True)
                    if r2.returncode != 0:
                        st.error(f"Twin runner failed:\n```\n{r2.stderr}\n```")
                        status.update(label="Failed", state="error")
                    else:
                        status.update(label="Complete ✅", state="complete")
                        st.cache_data.clear()
                        st.rerun()

        # ---- Read results from the twin DB if it exists -------------------
        import os as _os, sqlite3 as _sqlite3

        if _os.path.exists(twin_db):
            try:
                con = _sqlite3.connect(twin_db)
                df_runs = pd.read_sql_query(
                    "SELECT * FROM twin_runs ORDER BY timestamp DESC", con
                )
                con.close()
            except Exception as e:
                df_runs = pd.DataFrame()
                st.warning(f"Could not read twin_runs: {e}")
        else:
            df_runs = pd.DataFrame()

        if df_runs.empty:
            st.info(
                f"No twin run yet for **{scenario}**. "
                "Press **▶ Run Twin** above to generate and simulate."
            )
        else:
            latest_ts = df_runs["timestamp"].iloc[0] if "timestamp" in df_runs.columns else "—"
            st.caption(f"Last run: {latest_ts}")

            # --- Policy comparison table -----------------------------------
            st.subheader("Policy Comparison")
            POLICY_COLS = {
                "policy": "Policy",
                "dollars_per_month": "$/month",
                "worst_breach_pct": "Worst breach %",
                "saving_pct": "Saving %",
                "sla_met": "SLA met",
            }
            display_cols = [c for c in POLICY_COLS if c in df_runs.columns]
            if display_cols:
                display_df = df_runs[display_cols].rename(columns=POLICY_COLS)
                # Highlight reactive_p95 row
                def _highlight(row):
                    is_reactive = str(row.get("Policy", "")).lower().startswith("reactive")
                    bg = "background-color: rgba(0,184,148,0.15)" if is_reactive else ""
                    return [bg] * len(row)

                st.dataframe(
                    display_df.style.apply(_highlight, axis=1),
                    hide_index=True,
                    width="stretch",
                )

            # --- Bar chart of monthly cost per policy ----------------------
            if "policy" in df_runs.columns and "dollars_per_month" in df_runs.columns:
                st.subheader("Cost by Policy")
                chart_df = df_runs[["policy", "dollars_per_month"]].copy()
                chart_df = chart_df.set_index("policy").sort_values("dollars_per_month")
                st.bar_chart(chart_df)

            # --- Breach rate vs saving scatter -----------------------------
            if {"worst_breach_pct", "saving_pct", "policy"}.issubset(df_runs.columns):
                st.subheader("Breach Rate vs Saving")
                scatter_df = df_runs[["policy", "worst_breach_pct", "saving_pct"]].copy()
                scatter_df = scatter_df.set_index("policy")
                st.scatter_chart(scatter_df, x="worst_breach_pct", y="saving_pct")

            # --- Full run log (expander) -----------------------------------
            with st.expander("Full run log"):
                st.dataframe(df_runs, hide_index=True, width="stretch")


# ----------------------------------------------------------------------
# Data Health
# ----------------------------------------------------------------------
if tab_health is not None:
    with tab_health:
        st.header("Data health")

        checks = quality_checks(run["run_id"])
        if not checks.empty:
            passed = int((checks["status"] == "PASS").sum())
            warned = int((checks["status"] == "WARN").sum())
            failed = int((checks["status"] == "FAIL").sum())
            columns = st.columns(3)
            columns[0].metric("Passed", passed)
            columns[1].metric("Warnings", warned)
            columns[2].metric("Failures", failed)

            def colour(row):
                shade = {"PASS": "#1b3a1b", "WARN": "#3d3416", "FAIL": "#4a1a1a"}
                return [f"background-color: {shade.get(row['status'], '')}"] * len(row)

            st.dataframe(checks.style.apply(colour, axis=1),
                         width="stretch", hide_index=True)

        st.subheader("Collection gaps")
        from pipeline.validate import find_gaps

        gaps = find_gaps(frame)
        if gaps.empty:
            st.success("Continuous collection — no gaps detected.")
        else:
            st.warning(
                f"{len(gaps)} break(s) in collection. Every lag and rolling "
                f"window is computed inside a segment, so no feature reaches "
                f"across one."
            )
            st.dataframe(gaps, width="stretch", hide_index=True)

        st.subheader("Segments")
        from pipeline.clean import segment_profile

        st.dataframe(segment_profile(frame), width="stretch", hide_index=True)

        st.subheader("Provenance of the cleaned rows")
        columns = st.columns(3)
        columns[0].metric("Measured", int((frame["is_imputed"] == 0).sum()))
        columns[1].metric("Imputed (regrid)", int(frame["is_imputed"].sum()))
        columns[2].metric("Flagged outliers", int(frame["is_outlier"].sum()))
        st.caption(
            "Outliers are flagged, never deleted. CPU here moves between 0% and "
            "100% within a few samples, and those transitions are exactly the "
            "events an SLA analysis exists to capture."
        )

        st.subheader("ETL run history")
        st.dataframe(run_history(), width="stretch", hide_index=True)


# ----------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------
if tab_model is not None:
    with tab_model:
        st.header("Model performance")

        st.subheader("Baseline ladder")
        st.caption(
            "Every predictor on the identical test window. A model is only "
            "worth deploying if it beats the strongest simple alternative — "
            "not the most convenient one."
        )
        choice = st.selectbox("Resource", targets())
        table = ladder(frame, choice)
        if not table.empty:
            st.dataframe(table, width="stretch", hide_index=True)
            best = table.iloc[0]
            st.success(f"Strongest predictor: **{best['baseline']}** "
                       f"at MAE {best['mae']}")

            rows = table.set_index("baseline")["mae"]
            if "persistence" in rows and "persistence_lag1" in rows:
                st.warning(
                    f"`persistence` (next = current) scores {rows['persistence']}. "
                    f"`persistence_lag1` — predicting from the sample BEFORE the "
                    f"current one — scores {rows['persistence_lag1']}. An earlier "
                    f"version of this project measured against the second and "
                    f"reported the difference as model accuracy."
                )

        st.subheader("Model registry and promotion decisions")
        models = registry()
        if not models.empty:
            st.dataframe(
                models[["target", "algorithm", "mae", "baseline_mae",
                        "is_champion", "feature_version", "rejected_reason"]],
                width="stretch", hide_index=True,
            )
            rejected = models[models["rejected_reason"].notna()]
            if not rejected.empty:
                st.info(
                    f"{len(rejected)} model(s) rejected by the promotion gate. "
                    f"A challenger that loses to the baseline never reaches "
                    f"production — that is the gate working, not failing."
                )

        st.subheader("Drift monitor")
        from serving.drift import events

        drift_events = events()
        if drift_events.empty:
            st.caption("No drift events recorded yet.")
        else:
            st.dataframe(drift_events, width="stretch", hide_index=True)

        st.subheader("Prediction accuracy")
        from serving.predictor import recent_predictions

        predictions = recent_predictions(choice, limit=window)
        if not predictions.empty:
            predictions["ts"] = pd.to_datetime(predictions["ts"])
            st.line_chart(predictions.set_index("ts")[["actual", "predicted"]])
            st.caption(f"{len(predictions)} scored predictions, "
                       f"mean absolute error {predictions['abs_error'].mean():.4f}")


# ----------------------------------------------------------------------
# Cost & SLA
# ----------------------------------------------------------------------
if tab_cost is not None:
    with tab_cost:
        st.header("Cost and SLA")

        st.subheader("Walk-forward backtest")
        st.caption(
            "Every allocation decision re-made using only the data that existed "
            "at that moment, then charged for the window that followed."
        )

        with st.spinner("Replaying allocation decisions..."):
            results, node = backtest(frame)

        if not node.empty:
            st.dataframe(node, width="stretch", hide_index=True)
            compliant = node[node["sla_met"] & (node["policy"] != "static_100")]
            if not compliant.empty:
                best = compliant.sort_values("monthly_cost").iloc[0]
                st.success(
                    f"Cheapest SLA-compliant policy: **{best['policy']}** at "
                    f"${best['monthly_cost']}/mo — {best['savings_pct']}% saved, "
                    f"worst breach {best['worst_breach_rate']}%"
                )
                if best["policy"] == "reactive_p95":
                    st.warning(
                        "The winning policy uses **no model at all** — a trailing "
                        "P95 of recent demand. On this data the saving comes from "
                        "the allocation policy, not from forecasting."
                    )

        for target, result in results.items():
            if "error" in result:
                continue
            with st.expander(f"{target} — {result['decisions']} decisions"):
                st.dataframe(result["summary"], width="stretch", hide_index=True)

        st.subheader("Cost vs SLA tradeoff")
        from service.recommender import tradeoff_curve

        target = st.selectbox("Resource ", targets(), key="cost_target")
        curve, minimum = tradeoff_curve(target, frame)
        if curve is not None:
            st.line_chart(curve.set_index("allocation_percent")
                          [["monthly_cost", "breach_rate"]])
            if minimum:
                st.info(
                    f"Cheapest allocation meeting the "
                    f"{config.get_float('policy.max_breach_rate')}% SLA: "
                    f"**{minimum['allocation_percent']}%** at "
                    f"${minimum['monthly_cost']}/mo "
                    f"({minimum['breach_rate']}% breaches)"
                )


# ----------------------------------------------------------------------
# Lineage & Config
# ----------------------------------------------------------------------
if tab_lineage is not None:
    with tab_lineage:
        st.header("Lineage")

        from tracking.lineage import trace_model

        champion_table = champions()
        if not champion_table.empty:
            selected = st.selectbox("Model", champion_table["model_id"].tolist())
            trace = trace_model(selected)

            columns = st.columns(3)
            columns[0].metric("Feature version", trace.get("feature_version", "-"))
            columns[1].metric("Data fingerprint", trace.get("data_fingerprint", "-"))
            columns[2].metric("ETL run", trace.get("etl_run_id", "-"))

            if "etl_run" in trace:
                st.json(trace["etl_run"])
            if trace.get("quality_checks"):
                warned = [c for c in trace["quality_checks"] if c["status"] != "PASS"]
                if warned:
                    st.warning(f"{len(warned)} quality warning(s) applied to the "
                               f"data this model trained on:")
                    st.dataframe(pd.DataFrame(warned), width="stretch",
                                 hide_index=True)

        st.header("Configuration")
        st.caption(
            "Every value below lives in the `config` table. Nothing here is "
            "hardcoded — the only exception in the whole system is the database "
            "path itself, which has to exist before the table can be read."
        )

        from crud import config_crud

        category = st.selectbox("Category", config_crud.categories())
        rows = config_crud.describe(category)
        st.dataframe(
            pd.DataFrame(rows, columns=["key", "value", "type", "category",
                                        "description", "updated_at"])
            [["key", "value", "type", "description"]],
            width="stretch", hide_index=True,
        )

        st.subheader("Change a value")
        st.caption("Edit a setting and the next pipeline run uses it. "
                   "Every change is recorded in `config_history`.")

        editable = [r[0] for r in rows]
        key = st.selectbox("Setting", editable)
        current = config.get(key)
        new_value = st.text_input("New value", value=str(current))

        if st.button("Apply"):
            try:
                config.set_value(key, new_value, source="dashboard")
                st.cache_data.clear()
                st.success(f"`{key}` = {new_value}. "
                           f"Config fingerprint is now `{config.fingerprint()}`.")
                st.rerun()
            except Exception as exc:                           # noqa: BLE001
                st.error(f"{type(exc).__name__}: {exc}")

        st.subheader("Recent configuration changes")
        history = config_crud.history(limit=15)
        if history:
            st.dataframe(
                pd.DataFrame(history, columns=["key", "from", "to", "at", "source"]),
                width="stretch", hide_index=True,
            )


# ----------------------------------------------------------------------
# Logs — every recorded event, one stream
# ----------------------------------------------------------------------
if tab_logs is not None:
    with tab_logs:
        st.header("Event log")
        st.caption(
            "Six tables — `pipeline_runs`, `quality_checks`, `model_versions`, "
            "`drift_events`, `config_history` and process alerts — flattened "
            "into one chronological stream. There is no log file anywhere in "
            "this system: every line below is a row the pipeline wrote, which "
            "is why the log and the artifacts cannot disagree."
        )

        log = event_log(1000)

        if log.empty:
            st.info("No events recorded yet. Run `python -m orchestration.run_pipeline`.")
        else:
            errors = int((log["level"] == "ERROR").sum())
            warns = int((log["level"] == "WARN").sum())
            counters = st.columns(4)
            counters[0].metric("Events", len(log))
            counters[1].metric("Errors", errors)
            counters[2].metric("Warnings", warns)
            counters[3].metric("Most recent", log["at"].iloc[0])

            filters = st.columns([2, 2, 3])
            levels = filters[0].multiselect(
                "Level", ["ERROR", "WARN", "INFO"],
                default=["ERROR", "WARN", "INFO"],
            )
            sources = filters[1].multiselect(
                "Source", sorted(log["source"].unique()),
                default=sorted(log["source"].unique()),
            )
            search = filters[2].text_input(
                "Search", placeholder="substring of the event or its detail"
            )

            view = log[log["level"].isin(levels) & log["source"].isin(sources)]
            if search:
                needle = search.lower()
                view = view[
                    view["event"].str.lower().str.contains(needle, na=False)
                    | view["detail"].str.lower().str.contains(needle, na=False)
                ]

            st.caption(f"{len(view)} of {len(log)} events")
            st.dataframe(
                view, width="stretch", hide_index=True, height=460,
                column_config={
                    "at": st.column_config.TextColumn("When", width="small"),
                    "source": st.column_config.TextColumn("Source", width="small"),
                    "level": st.column_config.TextColumn("Level", width="small"),
                    "event": st.column_config.TextColumn("Event", width="medium"),
                    "detail": st.column_config.TextColumn("Detail", width="large"),
                },
            )

            st.download_button(
                "Download filtered log (CSV)",
                view.to_csv(index=False).encode("utf-8"),
                file_name="event_log.csv",
                mime="text/csv",
            )

            # The detail column is where the reasoning lives — a rejection
            # reason runs to a full sentence and gets truncated in the grid.
            with st.expander("Read one event in full"):
                if not view.empty:
                    labels = [
                        f"{r.at}  [{r.level}] {r.event}"
                        for r in view.itertuples()
                    ]
                    picked = st.selectbox("Event", labels, key="log_detail")
                    row = view.iloc[labels.index(picked)]
                    st.markdown(f"**{row['event']}** — `{row['source']}` / "
                                f"`{row['level']}` at `{row['at']}`")
                    st.code(row["detail"] or "(no detail)", language=None)

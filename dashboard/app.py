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

Presentation lives in two places and nowhere else: `.streamlit/config.toml`
for everything Streamlit themes natively, and `dashboard/theme.py` for the
CSS and components on top. This file should contain no colours, no font
sizes and no emoji — if you find yourself adding one, it belongs there.
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
from dashboard import theme                                 # noqa: E402
from dashboard.auth import require_login, logout            # noqa: E402

st.set_page_config(
    page_title="Predictive Resource Monitor",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.apply()

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
st.sidebar.markdown(
    '<div class="rm-title" style="font-size:1.0625rem">'
    'Predictive Resource Monitor</div>',
    unsafe_allow_html=True,
)
st.sidebar.markdown('<div class="rm-side-label">Signed in</div>',
                    unsafe_allow_html=True)
st.sidebar.markdown(
    f'<div class="rm-side-kv"><span>{session["email"]}</span>'
    f'<span>{role}</span></div>',
    unsafe_allow_html=True,
)
if st.sidebar.button("Sign out", width="stretch"):
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
_pod = os.environ.get("HOSTNAME")

st.sidebar.markdown('<div class="rm-side-label">Environment</div>',
                    unsafe_allow_html=True)
if _environment:
    _levels = {"production": "FAIL", "qs": "WARN", "dev": "INFO"}
    st.sidebar.markdown(
        theme.status_tag(_environment.upper(),
                         _levels.get(_environment, "INFO")),
        unsafe_allow_html=True,
    )
    if _pod:
        st.sidebar.markdown(
            f'<div class="rm-side-kv"><span>pod</span><span>{_pod}</span></div>',
            unsafe_allow_html=True,
        )
else:
    st.sidebar.markdown(theme.status_tag("LOCAL", "INFO"),
                        unsafe_allow_html=True)

run = latest_run()
if run is None:
    st.error("No successful ETL run. Run `python -m orchestration.run_pipeline` first.")
    st.stop()

st.sidebar.markdown('<div class="rm-side-label">Current run</div>',
                    unsafe_allow_html=True)
st.sidebar.markdown(
    "".join(
        f'<div class="rm-side-kv"><span>{k}</span><span>{v}</span></div>'
        for k, v in [
            ("ETL run", run["run_id"]),
            ("Started", run["started_at"]),
            ("Data", run["data_fingerprint"]),
            ("Config", config.fingerprint()),
        ]
    ),
    unsafe_allow_html=True,
)
st.sidebar.markdown(
    f'<div class="rm-side-kv"><span>Gate</span><span>'
    f'{theme.status_tag(run["gate_verdict"])}</span></div>',
    unsafe_allow_html=True,
)

from streamlit_autorefresh import st_autorefresh

st.sidebar.markdown('<div class="rm-side-label">Display</div>',
                    unsafe_allow_html=True)
window = st.sidebar.slider("Chart window (samples)", 50, 800, 300, step=50)

auto_refresh = st.sidebar.toggle("Auto-refresh live data", value=True)
if auto_refresh:
    st_autorefresh(interval=10000, key="data_refresh")
    st.cache_data.clear()
    config.invalidate()
elif st.sidebar.button("Refresh now", width="stretch"):
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
CUSTOMER_TABS = ["Overview", "Forecast", "Capacity", "Simulation"]
ADMIN_TABS = ["Data Health", "Model", "Cost & SLA", "Lineage & Config", "Logs"]

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
        theme.page(
            "Resource allocation and system state",
            "The smallest allocation the forecast supports without breaching "
            "the SLA, priced against static over-provisioning.",
        )

        from service.recommender import build_recommendation

        recommendation = build_recommendation(frame, persist=False)

        theme.section("Financial position")
        columns = st.columns(4)
        columns[0].metric("Static allocation",
                          f"${recommendation['static_cost']['total']}/mo")
        columns[1].metric("Predictive allocation",
                          f"${recommendation['predictive_cost']['total']}/mo")
        columns[2].metric("Snapshot saving",
                          f"${recommendation['monthly_savings']}",
                          f"{recommendation['savings_percent']}%")
        columns[3].metric("Samples", f"{len(frame)}",
                          f"{frame['segment_id'].nunique()} segments",
                          delta_color="off")

        theme.note(
            "The snapshot saving is a single instant. The measured figure is "
            "under Cost &amp; SLA, which replays every allocation decision — on "
            "this data the two disagree, and the replay is the one to trust."
        )

        theme.section("Recommended allocations")

        unit_names = {
            "cpu_percent": "CPU",
            "mem_percent": "Memory",
            "disk_read_mb_s": "Storage I/O",
        }

        rec_cols = st.columns(3)
        summary_rows = []
        for idx, (target, r) in enumerate(recommendation["recommendations"].items()):
            with rec_cols[idx % 3]:
                st.metric(
                    label=unit_names.get(target, target),
                    value=f"{r['recommended_percent']}%",
                    delta=f"{r['units']} {r['unit_label']}",
                    delta_color="off",
                )
                st.markdown(
                    theme.status_tag(
                        "Safety floor applied" if r["floored"] else "Within forecast",
                        "WARN" if r["floored"] else "PASS",
                    ),
                    unsafe_allow_html=True,
                )
                theme.kv([
                    ("Predictor", r["predictor"]),
                    ("Forecast peak", f"{r['forecast_peak']}%"),
                    ("Forecast P95", f"{r['forecast_p95']}%"),
                    ("Monthly cost", f"${r['monthly_cost']}"),
                    ("Static cost", f"${r['static_cost']}"),
                ])

            summary_rows.append({
                "Resource": target,
                "Allocation": f"{r['recommended_percent']}%",
                "Units": f"{r['units']} {r['unit_label']}",
                "Forecast peak": f"{r['forecast_peak']}%",
                "Predictor": r["predictor"],
                "Monthly cost": f"${r['monthly_cost']}",
                "Static cost": f"${r['static_cost']}",
                "Safety floor": r["floor_reason"] if r["floored"] else "Not applied",
            })

        st.dataframe(pd.DataFrame(summary_rows), width="stretch", hide_index=True)

        alerts = latest_process_alerts()
        if alerts:
            latest_alert = alerts[0]
            st.warning(
                f"Memory headroom alert at {latest_alert[0]} — live usage "
                f"crossed the threshold at {latest_alert[1]:.1f}%."
            )
            import json
            try:
                proc_df = pd.DataFrame(json.loads(latest_alert[2]))
                proc_df.columns = ["PID", "Process", "Memory (MB)"]
                st.dataframe(proc_df, hide_index=True, width="stretch")
            except Exception as exc:                              # noqa: BLE001
                theme.note(f"Could not parse the process list: {exc}")

        theme.section("Champions serving production")
        st.dataframe(champions(), width="stretch", hide_index=True)

        theme.section("Utilisation")
        recent = frame.tail(window)
        resources = targets()
        st.line_chart(
            recent.set_index("ts")[resources],
            color=theme.series(len(resources)),
            height=280,
        )

        theme.section("Forecast against allocation")
        from serving.predictor import forecast_horizon

        for target, r in recommendation["recommendations"].items():
            theme.sub(unit_names.get(target, target))
            left, right = st.columns([3, 2])
            with left:
                trajectory, meta = forecast_horizon(target, df=frame)
                actual_df = recent.copy()
                actual_df["ts"] = pd.to_datetime(actual_df["ts"])
                actual_df = actual_df.set_index("ts")[[target]].rename(
                    columns={target: "Actual"})
                actual_df["Allocated"] = r["recommended_percent"]

                if not trajectory.empty:
                    traj_df = trajectory.copy()
                    traj_df["ts"] = pd.to_datetime(traj_df["ts"])
                    traj_df = traj_df.set_index("ts")[["predicted"]].rename(
                        columns={"predicted": "Forecast"})
                    last_ts = actual_df.index[-1]
                    actual_df.loc[last_ts, "Forecast"] = actual_df["Actual"].iloc[-1]
                    chart = pd.concat([actual_df, traj_df], axis=0)
                else:
                    chart = actual_df

                # Columns are ordered so the slot assignment is stable
                # whether or not a forecast was produced.
                order = [c for c in ["Actual", "Forecast", "Allocated"]
                         if c in chart.columns]
                st.line_chart(chart[order], color=theme.series(len(order)),
                              height=240)
            with right:
                st.metric("Allocation", f"{r['recommended_percent']}%",
                          f"{r['units']} {r['unit_label']}", delta_color="off")
                theme.kv([
                    ("Forecast peak", f"{r['forecast_peak']}%"),
                    ("Forecast P95", f"{r['forecast_p95']}%"),
                    ("Forecast asked for", f"{r['forecast_alloc']}%"),
                    ("Safety floor",
                     r["floor_reason"] if r["floored"] else "Not applied"),
                    ("Breaches",
                     f"{r['breach_rate']}% in {r['breach_episodes']} episode(s)"),
                ])


# ----------------------------------------------------------------------
# Forecast Studio
# ----------------------------------------------------------------------
if tab_forecast is not None:
    with tab_forecast:
        theme.page(
            "Forecast",
            "Multi-step horizon forecast with a 95% uncertainty band, and a "
            "stress test that re-forecasts against a simulated demand spike.",
        )

        col_ctrl1, col_ctrl2, col_ctrl3 = st.columns([2, 2, 2])
        with col_ctrl1:
            fc_target = st.selectbox("Resource", targets(), key="fc_studio_target")
        with col_ctrl2:
            fc_steps = st.slider("Horizon (steps)", 10, 100, 30, step=5,
                                 key="fc_studio_steps")
        with col_ctrl3:
            show_bounds = st.checkbox("Show 95% uncertainty band", value=True,
                                      key="fc_studio_bounds")

        from serving.predictor import forecast_horizon, resolve_champion
        from service.recommender import recommend_percent

        trajectory, meta = forecast_horizon(fc_target, steps=fc_steps, df=frame)
        r = recommend_percent(fc_target, df=frame)

        if not trajectory.empty:
            cadence = config.get_int("pipeline.nominal_cadence_sec")
            horizon_sec = len(trajectory) * cadence

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Projected peak", f"{meta['forecast_peak']}%")
            m2.metric("Projected P95", f"{meta['forecast_p95']}%")
            m3.metric("Window", f"{horizon_sec}s",
                      f"{len(trajectory)} steps", delta_color="off")
            m4.metric("Champion", meta["model_id"].split("-")[0],
                      f"MAE {meta.get('mae', 0.0):.3f}", delta_color="off")

            # Main Forecast Trajectory Chart
            theme.section(f"Trajectory — {fc_target}, {horizon_sec}s horizon")

            recent_fc = frame.tail(window).copy()
            recent_fc["ts"] = pd.to_datetime(recent_fc["ts"])
            actual_chart = recent_fc.set_index("ts")[[fc_target]].rename(
                columns={fc_target: "Actual"})

            traj_chart = trajectory.copy()
            traj_chart["ts"] = pd.to_datetime(traj_chart["ts"])

            banded = show_bounds and "upper_bound" in traj_chart.columns
            if banded:
                traj_chart = traj_chart.set_index("ts")[
                    ["predicted", "upper_bound", "lower_bound"]].rename(columns={
                        "predicted": "Forecast",
                        "upper_bound": "Upper 95%",
                        "lower_bound": "Lower 95%",
                    })
            else:
                traj_chart = traj_chart.set_index("ts")[["predicted"]].rename(
                    columns={"predicted": "Forecast"})

            # Bridge point — without it the forecast starts detached from the
            # last measurement and reads as a separate, unrelated series.
            last_ts = actual_chart.index[-1]
            last_val = actual_chart["Actual"].iloc[-1]
            actual_chart.loc[last_ts, "Forecast"] = last_val
            if banded:
                actual_chart.loc[last_ts, "Upper 95%"] = last_val
                actual_chart.loc[last_ts, "Lower 95%"] = last_val

            if r:
                actual_chart["Allocated"] = r["recommended_percent"]

            combined_fc_chart = pd.concat([actual_chart, traj_chart], axis=0)

            # Two data series (slots 1-2), the band as a lighter step of the
            # forecast's own hue, and the allocation as muted annotation ink.
            fc_order, fc_colours = ["Actual", "Forecast"], theme.series(2)
            if banded:
                fc_order += ["Upper 95%", "Lower 95%"]
                fc_colours += [theme.BAND, theme.BAND]
            if "Allocated" in combined_fc_chart.columns:
                fc_order += ["Allocated"]
                fc_colours += [theme.REFERENCE]

            st.line_chart(combined_fc_chart[fc_order], color=fc_colours,
                          height=340)

            exp_left, exp_right = st.columns(2)
            with exp_left:
                theme.sub("Champion and lineage")
                theme.kv([
                    ("Predictor", meta.get("predictor", "—")),
                    ("Champion", meta.get("model_id", "—")),
                    ("Cadence", f"{cadence}s per sample"),
                    ("Horizon", f"{horizon_sec}s over {fc_steps} steps"),
                    ("Expected error (MAE)", meta.get("mae", "—")),
                ])
            with exp_right:
                theme.sub("Allocation impact")
                if r:
                    theme.kv([
                        ("Forecast asked for", f"{r['forecast_alloc']}%"),
                        ("Allocation",
                         f"{r['recommended_percent']}%  ({r['units']} {r['unit_label']})"),
                        ("Safety floor", r["floor_reason"]),
                        ("Monthly cost",
                         f"${r['monthly_cost']}  (static ${r['static_cost']})"),
                    ])

            theme.section("Demand spike stress test")
            theme.note(
                "Scale the recorded demand by a factor and re-run the "
                "forecaster on it. A model that tracks the spike is reading "
                "the signal; one that does not has learned the level instead "
                "of the shape."
            )
            sim_spike = st.slider("Spike factor", 1.0, 3.0, 1.5, step=0.1,
                                  key="sim_spike_slider")

            if st.button("Run stress test", type="primary"):
                spiked_frame = frame.copy()
                spiked_frame[fc_target] = spiked_frame[fc_target] * sim_spike
                spiked_traj, spiked_meta = forecast_horizon(
                    fc_target, steps=fc_steps, df=spiked_frame)
                if not spiked_traj.empty:
                    st.success(
                        f"Projected peak {spiked_meta['forecast_peak']}% "
                        f"against {meta['forecast_peak']}% unspiked."
                    )
                    spiked_chart = spiked_traj.set_index("step")[
                        ["predicted"]].rename(
                        columns={"predicted": f"Spiked {sim_spike}x"})
                    orig_chart = trajectory.set_index("step")[["predicted"]].rename(
                        columns={"predicted": "Baseline 1.0x"})
                    st.line_chart(pd.concat([orig_chart, spiked_chart], axis=1),
                                  color=theme.series(2), height=280)
        else:
            st.warning(
                f"No forecast could be generated for {fc_target}. Check data "
                f"health and the model registry."
            )


# ----------------------------------------------------------------------
# Capacity
# ----------------------------------------------------------------------
if tab_capacity is not None:
    with tab_capacity:
        theme.page(
            "Capacity",
            "Current utilisation, the forecast peak and the alert threshold "
            "for each resource, on one shared 0-100% scale.",
        )

        threshold_val = config.get_float("policy.capacity_alert_threshold", 80.0)

        def get_current_usage(target, fallback_df):
            """The absolute most recent raw metric from the collector."""
            try:
                from database.connection import get_connection
                con = get_connection()
                if con:
                    df = pd.read_sql_query(
                        "SELECT * FROM metrics ORDER BY ts DESC LIMIT 1", con)
                    con.close()
                    if not df.empty and target in df.columns:
                        return float(df[target].iloc[0])
            except Exception:                                     # noqa: BLE001
                pass

            if not fallback_df.empty and target in fallback_df.columns:
                return float(fallback_df[target].iloc[-1])
            return 0.0

        from service.recommender import build_recommendation
        recs = build_recommendation(frame, persist=False)

        resource_info = {
            "cpu_percent": {"name": "CPU", "capacity_key": "node.vcpus",
                            "unit": "vCPUs"},
            "mem_percent": {"name": "Memory", "capacity_key": "node.ram_gb",
                            "unit": "GB RAM"},
            "disk_read_mb_s": {"name": "Storage", "capacity_key": "node.storage_gb",
                               "unit": "GB"},
        }

        rows, over_threshold = [], []
        for target, info in resource_info.items():
            if target not in recs["recommendations"]:
                continue

            r = recs["recommendations"][target]
            capacity = config.get_int(info["capacity_key"])
            current_pct = get_current_usage(target, frame)
            forecast_pct = r["forecast_peak"]

            facts = [
                ("Current", f"{current_pct:.1f}%"),
                ("Forecast peak", f"{forecast_pct:.1f}%"),
                ("Threshold", f"{threshold_val:.0f}%"),
                ("Allocation", f"{r['recommended_percent']}% "
                               f"({r['units']:.2f} {info['unit']})"),
            ]
            if forecast_pct > threshold_val:
                over_threshold.append(info["name"])

            rows.append({
                "name": info["name"],
                "capacity_label": f"{capacity} {info['unit']} provisioned",
                "current": current_pct,
                "forecast": forecast_pct,
                "threshold": threshold_val,
                "facts": facts,
            })

        theme.section("Utilisation against capacity")
        theme.bullet(rows)
        theme.legend([
            (theme.SERIES[0], "Current utilisation"),
            (theme.INK, "Forecast peak"),
            (theme.CRITICAL, f"Alert threshold ({threshold_val:.0f}%)"),
        ])

        if over_threshold:
            st.warning(
                "Forecast peak crosses the alert threshold for: "
                + ", ".join(over_threshold)
                + ". The allocation below already includes the safety floor."
            )

        theme.section("Detail")
        detail_rows = []
        for target, info in resource_info.items():
            if target not in recs["recommendations"]:
                continue
            r = recs["recommendations"][target]
            capacity = config.get_int(info["capacity_key"])
            current_pct = get_current_usage(target, frame)
            detail_rows.append({
                "Resource": info["name"],
                "Capacity": f"{capacity} {info['unit']}",
                "Current": f"{current_pct:.1f}%",
                "Current (units)": f"{(current_pct / 100.0) * capacity:.2f}",
                "Forecast peak": f"{r['forecast_peak']:.1f}%",
                "Forecast (units)":
                    f"{(r['forecast_peak'] / 100.0) * capacity:.2f}",
                "Allocation": f"{r['recommended_percent']}%",
                "Allocation (units)": f"{r['units']:.2f}",
            })
        st.dataframe(pd.DataFrame(detail_rows), width="stretch", hide_index=True)



# ----------------------------------------------------------------------
# Digital Twin
# ----------------------------------------------------------------------
if tab_twin is not None:
    with tab_twin:
        theme.page(
            "Simulation",
            "Run the full twelve-stage pipeline against a synthetic load "
            "scenario in an isolated database, then compare every allocation "
            "policy on the result.",
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
                     "regime_change: idle to high, as a step | "
                     "sustained_spike: gradual ramp to 90%+ | "
                     "cadence_drift: sampling interval varies | "
                     "multi_host_shift: shifted baseline",
            )
        with col_btn:
            st.write("")
            st.write("")
            run_twin_btn = st.button("Run simulation", type="primary",
                                     width="stretch")

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
                        status.update(label="Complete", state="complete")
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
                f"No simulation has been run for {scenario} yet. "
                f"Use Run simulation above to generate and replay one."
            )
        else:
            latest_ts = (df_runs["timestamp"].iloc[0]
                         if "timestamp" in df_runs.columns else "—")

            theme.section("Policy comparison")
            theme.note(f"Last run {latest_ts}.")

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

                # The tint marks the policy that uses no model at all. It is
                # redundant with the Policy column it reads, deliberately —
                # the name carries the meaning, the tint only finds the row.
                def _highlight(row):
                    reactive = str(row.get("Policy", "")).lower().startswith("reactive")
                    tint = f"background-color: {theme.TINT_GOOD}"
                    return [tint if reactive else ""] * len(row)

                st.dataframe(display_df.style.apply(_highlight, axis=1),
                             hide_index=True, width="stretch")

            if {"policy", "dollars_per_month"}.issubset(df_runs.columns):
                theme.sub("Monthly cost by policy")
                chart_df = (df_runs[["policy", "dollars_per_month"]]
                            .set_index("policy")
                            .sort_values("dollars_per_month"))
                st.bar_chart(chart_df, color=theme.series(1), height=260,
                             horizontal=True)

            if {"worst_breach_pct", "saving_pct", "policy"}.issubset(df_runs.columns):
                theme.sub(
                    "Saving against worst breach",
                    "Up is cheaper, right is less reliable. The useful "
                    "policies sit top-left; anything far right buys its "
                    "saving out of the SLA budget.",
                )
                scatter_df = df_runs[["policy", "worst_breach_pct",
                                      "saving_pct"]].set_index("policy")
                st.scatter_chart(scatter_df, x="worst_breach_pct",
                                 y="saving_pct", color=theme.series(1),
                                 height=300)

            with st.expander("Full run log"):
                st.dataframe(df_runs, hide_index=True, width="stretch")


# ----------------------------------------------------------------------
# Data Health
# ----------------------------------------------------------------------
if tab_health is not None:
    with tab_health:
        theme.page(
            "Data health",
            "The quality gate for the current ETL run, and the collection "
            "record behind it.",
        )

        checks = quality_checks(run["run_id"])
        if not checks.empty:
            theme.section("Quality gate")
            columns = st.columns(3)
            columns[0].metric("Passed", int((checks["status"] == "PASS").sum()))
            columns[1].metric("Warnings", int((checks["status"] == "WARN").sum()))
            columns[2].metric("Failures", int((checks["status"] == "FAIL").sum()))

            st.dataframe(theme.status_frame(checks),
                         width="stretch", hide_index=True)

        theme.section("Collection gaps")
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

        theme.section("Segments")
        from pipeline.clean import segment_profile

        st.dataframe(segment_profile(frame), width="stretch", hide_index=True)

        theme.section("Provenance of the cleaned rows")
        columns = st.columns(3)
        columns[0].metric("Measured", int((frame["is_imputed"] == 0).sum()))
        columns[1].metric("Imputed on regrid", int(frame["is_imputed"].sum()))
        columns[2].metric("Flagged outliers", int(frame["is_outlier"].sum()))
        theme.note(
            "Outliers are flagged, never deleted. CPU here moves between 0% "
            "and 100% within a few samples, and those transitions are exactly "
            "the events an SLA analysis exists to capture."
        )

        theme.section("ETL run history")
        st.dataframe(run_history(), width="stretch", hide_index=True)


# ----------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------
if tab_model is not None:
    with tab_model:
        theme.page(
            "Model",
            "What the promotion gate measured, and what it decided.",
        )

        choice = st.selectbox("Resource", targets())

        theme.section("Baseline ladder")
        theme.note(
            "Every predictor on the identical test window. A model is only "
            "worth deploying if it beats the strongest simple alternative — "
            "not the most convenient one."
        )
        table = ladder(frame, choice)
        if not table.empty:
            st.dataframe(table, width="stretch", hide_index=True)
            best = table.iloc[0]
            st.success(f"Strongest predictor: {best['baseline']} "
                       f"at MAE {best['mae']}.")

            rows = table.set_index("baseline")["mae"]
            if "persistence" in rows and "persistence_lag1" in rows:
                st.warning(
                    f"persistence (next = current) scores {rows['persistence']}. "
                    f"persistence_lag1 — predicting from the sample before the "
                    f"current one — scores {rows['persistence_lag1']}. An "
                    f"earlier version of this project measured against the "
                    f"second and reported the difference as model accuracy."
                )

        theme.section("Registry and promotion decisions")
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

        theme.section("Drift monitor")
        from serving.drift import events

        drift_events = events()
        if drift_events.empty:
            theme.note("No drift events recorded yet.")
        else:
            st.dataframe(drift_events, width="stretch", hide_index=True)

        theme.section("Prediction accuracy")
        from serving.predictor import recent_predictions

        predictions = recent_predictions(choice, limit=window)
        if not predictions.empty:
            predictions["ts"] = pd.to_datetime(predictions["ts"])
            accuracy = predictions.set_index("ts")[["actual", "predicted"]]
            accuracy.columns = ["Actual", "Predicted"]
            st.line_chart(accuracy, color=theme.series(2), height=280)
            theme.note(
                f"{len(predictions)} scored predictions, mean absolute error "
                f"{predictions['abs_error'].mean():.4f}."
            )


# ----------------------------------------------------------------------
# Cost & SLA
# ----------------------------------------------------------------------
if tab_cost is not None:
    with tab_cost:
        theme.page(
            "Cost and SLA",
            "Every allocation decision replayed against the recorded data, "
            "then priced. This is the measured number; the Overview snapshot "
            "is not.",
        )

        theme.section("Walk-forward backtest")
        theme.note(
            "Every allocation decision re-made using only the data that "
            "existed at that moment, then charged for the window that followed."
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

        theme.section("Cost against SLA")
        from service.recommender import tradeoff_curve

        target = st.selectbox("Resource ", targets(), key="cost_target")
        curve, minimum = tradeoff_curve(target, frame)
        if curve is not None:
            # Two charts, not two y-axes on one. Dollars run in the hundreds
            # and breach rate is a percentage: plotted together on a single
            # scale the breach line flattens onto the axis and the crossover
            # — the entire point of the figure — becomes invisible.
            indexed = curve.set_index("allocation_percent")

            theme.sub("Monthly cost by allocation")
            st.line_chart(indexed[["monthly_cost"]].rename(
                columns={"monthly_cost": "Monthly cost ($)"}),
                color=theme.series(1), height=220)

            theme.sub("Breach rate by allocation")
            st.line_chart(indexed[["breach_rate"]].rename(
                columns={"breach_rate": "Breach rate (%)"}),
                color=[theme.SERIES[1]], height=220)

            if minimum:
                st.info(
                    f"Cheapest allocation meeting the "
                    f"{config.get_float('policy.max_breach_rate')}% SLA: "
                    f"{minimum['allocation_percent']}% at "
                    f"${minimum['monthly_cost']}/mo, "
                    f"{minimum['breach_rate']}% breaches."
                )


# ----------------------------------------------------------------------
# Lineage & Config
# ----------------------------------------------------------------------
if tab_lineage is not None:
    with tab_lineage:
        theme.page(
            "Lineage and configuration",
            "Trace any champion back to the data, features and settings that "
            "produced it — and change those settings in place.",
        )

        from tracking.lineage import trace_model

        theme.section("Model lineage")
        champion_table = champions()
        if not champion_table.empty:
            selected = st.selectbox("Model", champion_table["model_id"].tolist())
            trace = trace_model(selected)

            columns = st.columns(3)
            columns[0].metric("Feature version", trace.get("feature_version", "—"))
            columns[1].metric("Data fingerprint", trace.get("data_fingerprint", "—"))
            columns[2].metric("ETL run", trace.get("etl_run_id", "—"))

            if "etl_run" in trace:
                with st.expander("ETL run record"):
                    st.json(trace["etl_run"])
            if trace.get("quality_checks"):
                warned = [c for c in trace["quality_checks"]
                          if c["status"] != "PASS"]
                if warned:
                    st.warning(
                        f"{len(warned)} quality warning(s) applied to the data "
                        f"this model trained on."
                    )
                    st.dataframe(theme.status_frame(pd.DataFrame(warned)),
                                 width="stretch", hide_index=True)

        theme.section("Configuration")
        theme.note(
            "Every value below lives in the config table. Nothing here is "
            "hardcoded — the only exception in the whole system is the "
            "database path itself, which has to exist before the table can be "
            "read."
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

        theme.sub(
            "Change a value",
            "The next pipeline run uses it, and the change is recorded in "
            "config_history.",
        )

        editable = [r[0] for r in rows]
        edit_left, edit_right = st.columns(2)
        with edit_left:
            key = st.selectbox("Setting", editable)
        with edit_right:
            current = config.get(key)
            new_value = st.text_input("New value", value=str(current))

        if st.button("Apply change", type="primary"):
            try:
                config.set_value(key, new_value, source="dashboard")
                st.cache_data.clear()
                st.success(f"{key} = {new_value}. Config fingerprint is now "
                           f"{config.fingerprint()}.")
                st.rerun()
            except Exception as exc:                           # noqa: BLE001
                st.error(f"{type(exc).__name__}: {exc}")

        theme.sub("Recent changes")
        history = config_crud.history(limit=15)
        if history:
            st.dataframe(
                pd.DataFrame(history,
                             columns=["key", "from", "to", "at", "source"]),
                width="stretch", hide_index=True,
            )


# ----------------------------------------------------------------------
# Logs — every recorded event, one stream
# ----------------------------------------------------------------------
if tab_logs is not None:
    with tab_logs:
        theme.page(
            "Event log",
            "Six tables — pipeline runs, quality checks, model versions, "
            "drift events, config history and process alerts — flattened into "
            "one chronological stream. There is no log file anywhere in this "
            "system: every line below is a row the pipeline wrote, which is "
            "why the log and the artifacts cannot disagree.",
        )

        log = event_log(1000)

        if log.empty:
            st.info("No events recorded yet. Run the pipeline first.")
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

            theme.note(f"{len(view)} of {len(log)} events.")
            st.dataframe(
                theme.status_frame(view, column="level"),
                width="stretch", hide_index=True, height=460,
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

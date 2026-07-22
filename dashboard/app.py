"""
Dashboard — the twelve-stage pipeline, visible.

    streamlit run dashboard/app.py

Five tabs, all reading from the database rather than recomputing:

    Overview     champions, live utilisation, allocation and cost
    Data Health  quality gate, collection gaps, segments, cleaning audit
    Model        baseline ladder, registry, promotion decisions, drift
    Cost & SLA   walk-forward backtest and the cost/breach tradeoff curve
    Lineage      trace any model to its data; browse and EDIT config

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

st.set_page_config(page_title="Predictive Resource Monitor", layout="wide")


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


def targets():
    return config.get_json("features.targets")


# ----------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------
st.sidebar.title("Predictive Resource Monitor")

run = latest_run()
if run is None:
    st.error("No successful ETL run. Run `python -m orchestration.run_pipeline` first.")
    st.stop()

st.sidebar.caption(f"ETL run **{run['run_id']}** — {run['started_at']}")
st.sidebar.caption(f"data `{run['data_fingerprint']}`")
st.sidebar.caption(f"config `{config.fingerprint()}`")
st.sidebar.caption(f"gate **{run['gate_verdict']}**")

window = st.sidebar.slider("Chart window (samples)", 50, 800, 300, step=50)
if st.sidebar.button("Refresh data"):
    st.cache_data.clear()
    st.rerun()

frame = clean_frame(run["run_id"])
if frame.empty:
    st.error("The latest run produced no cleaned rows.")
    st.stop()

tab_overview, tab_health, tab_model, tab_cost, tab_lineage = st.tabs([
    "Overview", "Data Health", "Model", "Cost & SLA", "Lineage & Config",
])


# ----------------------------------------------------------------------
# Overview
# ----------------------------------------------------------------------
with tab_overview:
    st.header("Current state")

    from service.recommender import build_recommendation

    recommendation = build_recommendation(frame, persist=False)

    columns = st.columns(4)
    columns[0].metric("Static (100%)",
                      f"${recommendation['static_cost']['total']}/mo")
    columns[1].metric("Predictive allocation",
                      f"${recommendation['predictive_cost']['total']}/mo")
    columns[2].metric("Snapshot saving",
                      f"${recommendation['monthly_savings']}",
                      f"{recommendation['savings_percent']}%")
    columns[3].metric("Samples", f"{len(frame)}",
                      f"{frame['segment_id'].nunique()} segments")

    st.info(
        "The snapshot saving is a single instant. The measured figure is in "
        "**Cost & SLA**, which replays every allocation decision — on this "
        "data the two disagree, and the replay is the one to trust."
    )

    st.subheader("Champions serving production")
    st.dataframe(champions(), width="stretch", hide_index=True)

    st.subheader("Utilisation")
    recent = frame.tail(window)
    st.line_chart(recent.set_index("ts")[targets()])

    st.subheader("Allocation vs demand")
    for target, r in recommendation["recommendations"].items():
        left, right = st.columns([3, 2])
        with left:
            chart = pd.DataFrame({
                "actual": recent.set_index("ts")[target],
                "allocated": r["recommended_percent"],
            })
            st.caption(f"**{target}** — {r['predictor']}")
            st.line_chart(chart)
        with right:
            st.metric(f"{target} allocation",
                      f"{r['recommended_percent']}%",
                      f"{r['units']} {r['unit_label']}")
            st.caption(f"forecast wanted {r['forecast_alloc']}%")
            if r["floored"]:
                st.caption(f"safety floor: {r['floor_reason']}")
            st.caption(f"breaches {r['breach_rate']}% "
                       f"in {r['breach_episodes']} episode(s)")


# ----------------------------------------------------------------------
# Data Health
# ----------------------------------------------------------------------
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
        st.line_chart(predictions.set_index("ts")[["actual", "predicted"]])
        st.caption(f"{len(predictions)} scored predictions, "
                   f"mean absolute error {predictions['abs_error'].mean():.4f}")


# ----------------------------------------------------------------------
# Cost & SLA
# ----------------------------------------------------------------------
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

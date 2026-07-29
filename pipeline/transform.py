"""
STAGE 6 — Data Transformation: standardisation, formatting, normalisation.

Cleaning (stage 5) produces a well-formed series. Transformation turns
that series into the representations the rest of the system consumes:

    add_derived     rate-of-change, volatility, smoothed level, headroom
    label_regimes   idle / ramp / saturated, from bounds held in config
    encode          the regime label as numbers a model can consume
    normalise_units MB -> GB and percent -> fraction, so units are stated
    rollup          retention tiers (15s / 1min / 5min)

Encoding runs here, immediately after labelling, because `regime` is
created two lines earlier and this is the stage whose job is making the
data consumable. The encoder itself lives in `pipeline/encode.py`; see
that module for why its category list comes from config rather than from
the data in front of it.

A note on stage 6 versus stage 9
--------------------------------
The slide separates "transformation (standardisation, formatting,
normalisation)" from "feature scaling (min-max, standard, robust)", and
they are genuinely different jobs. This module makes units and shapes
consistent for everybody — dashboards, cost model, humans reading a
table. `model/scaling.py` fits a scaler on training rows only, for one
estimator, and must never be applied here: doing so would leak test-set
statistics into everything downstream.

A note on rollups
-----------------
Each tier keeps mean, max AND p95. A mean alone would be the wrong
summary to retain: allocation decisions are driven by peaks, and a
one-minute mean hides exactly the spike that breaches an SLA.
"""

import numpy as np
import pandas as pd

import config
from pipeline import encode as encode_mod
from pipeline import schema as schema_mod
from pipeline.validate import detect_cadence


# ----------------------------------------------------------------------
# Derived columns
# ----------------------------------------------------------------------
def add_derived(df):
    """Add rate-of-change, volatility, smoothed level and headroom.

    Every window operation runs inside a groupby on `segment_id`, so
    nothing here can reach across a break in collection.
    """
    out = df.copy()
    window = config.get_int("features.roll_window")
    targets = schema_mod.target_columns()
    added = []

    for col in targets:
        if col not in out.columns:
            continue
        grouped = out.groupby("segment_id")[col]

        # First difference: how fast the resource is moving, and in which
        # direction. The level alone cannot distinguish 60% climbing from
        # 60% falling, and those warrant different allocations.
        out[f"{col}_delta"] = grouped.diff()

        # Rolling dispersion: a volatile resource needs more headroom than
        # a steady one at the same mean.
        out[f"{col}_roll_std"] = grouped.transform(
            lambda s: s.rolling(window, min_periods=2).std()
        )

        # Fast and slow exponential means. Their difference is a
        # trend signal that reacts sooner than a plain rolling mean.
        out[f"{col}_ewma_fast"] = grouped.transform(
            lambda s: s.ewm(span=max(2, window // 3), adjust=False).mean()
        )
        out[f"{col}_ewma_slow"] = grouped.transform(
            lambda s: s.ewm(span=window, adjust=False).mean()
        )
        out[f"{col}_trend"] = out[f"{col}_ewma_fast"] - out[f"{col}_ewma_slow"]

        # Distance to saturation, the quantity the allocator actually
        # cares about.
        out[f"{col}_headroom"] = 100.0 - out[col]

        added += [f"{col}_delta", f"{col}_roll_std", f"{col}_ewma_fast",
                  f"{col}_ewma_slow", f"{col}_trend", f"{col}_headroom"]

    return out, {"derived_columns_added": len(added), "derived_columns": added}


# ----------------------------------------------------------------------
# Regime labelling
# ----------------------------------------------------------------------
def label_regimes(df, column="cpu_percent"):
    """Tag each row idle / ramp / saturated using bounds from config.

    Error is not uniform across operating regimes — a model can be
    excellent at idle and poor during a ramp — and an average MAE hides
    that. Per-regime error reporting depends on this label.
    """
    out = df.copy()
    bounds = config.get_json("regime.bounds")

    if column not in out.columns:
        out["regime"] = "unknown"
        return out, {"regimes": {}}

    def classify(value):
        if pd.isna(value):
            return "unknown"
        for name, (low, high) in bounds.items():
            if low <= value < high:
                return name
        return "unknown"

    out["regime"] = out[column].apply(classify)
    counts = out["regime"].value_counts().to_dict()
    occupancy = {k: round(100.0 * v / len(out), 1) for k, v in counts.items()}
    return out, {"regimes": counts, "regime_occupancy_pct": occupancy}


# ----------------------------------------------------------------------
# Unit normalisation
# ----------------------------------------------------------------------
def normalise_units(df):
    """Make units explicit and consistent.

    `mem_used_mb` is converted to GB for consistency. Disk metrics are
    already in MB/s (read/write throughput) and need no conversion.
    """
    out = df.copy()
    added = []

    if "mem_used_mb" in out.columns:
        out["mem_used_gb"] = out["mem_used_mb"] / 1024.0
        added.append("mem_used_gb")

    for col in schema_mod.target_columns():
        if col in out.columns:
            out[f"{col}_fraction"] = out[col] / 100.0
            added.append(f"{col}_fraction")

    return out, {"unit_columns_added": added}


# ----------------------------------------------------------------------
# Rollups / retention tiers
# ----------------------------------------------------------------------
def rollup(df, freq="1min"):
    """Downsample to `freq`, keeping mean, max and p95 per column.

    Retaining only the mean would be the wrong compression for this
    system: allocation is driven by peaks, so p95 and max are the columns
    that must survive downsampling.
    """
    if df.empty:
        return pd.DataFrame()

    numeric = [c for c in schema_mod.value_columns() if c in df.columns]
    frame = df.set_index("ts")

    aggregated = frame[numeric].resample(freq).agg(
        ["mean", "max", lambda s: s.quantile(0.95)]
    )
    aggregated.columns = [
        f"{col}_{'p95' if 'lambda' in str(stat) else stat}"
        for col, stat in aggregated.columns
    ]
    aggregated["samples"] = frame[numeric[0]].resample(freq).size() if numeric else 0

    # Drop buckets that contain no observations. `resample` spans the full
    # range between the first and last timestamp, so a multi-day break in
    # collection would otherwise produce tens of thousands of empty rows —
    # the 15-second tier generated 29,396 of them across a five-day gap.
    # A bucket with no samples is an absence, not a measurement.
    return aggregated[aggregated["samples"] > 0].reset_index()


def build_rollups(df):
    """Every retention tier named in config."""
    tiers = {}
    for freq in config.get_json("pipeline.rollup_freqs"):
        tiers[freq] = rollup(df, freq)
    return tiers, {
        "rollup_tiers": {k: len(v) for k, v in tiers.items()}
    }


# ----------------------------------------------------------------------
# The whole stage
# ----------------------------------------------------------------------
def transform(df):
    """Run every transformation step. Returns (frame, rollups, report)."""
    report = {"rows_in": len(df)}

    df, r = add_derived(df);      report.update(r)
    df, r = label_regimes(df);    report.update(r)
    df, r = encode_mod.encode(df); report.update(r)
    df, r = normalise_units(df);  report.update(r)
    tiers, r = build_rollups(df); report.update(r)

    report["rows_out"] = len(df)
    report["columns_out"] = len(df.columns)
    return df, tiers, report


def format_report(report):
    lines = [
        "=" * 78,
        "DATA TRANSFORMATION",
        "=" * 78,
        f"  rows                 : {report['rows_in']} -> {report['rows_out']}",
        f"  columns out          : {report['columns_out']}",
        f"  derived columns      : {report['derived_columns_added']}",
        f"  unit columns         : {len(report['unit_columns_added'])} "
        f"{report['unit_columns_added']}",
        f"  regime occupancy     : {report['regime_occupancy_pct']}",
        f"  encoded columns      : {report.get('encoded_columns_added', 0)} "
        f"({report.get('encoding_method')})  {report.get('encoded_columns', [])}",
        f"  rollup tiers (rows)  : {report['rollup_tiers']}",
        "=" * 78,
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    from pipeline.clean import clean
    from pipeline.sources import get_source

    cleaned, _ = clean(get_source("sqlite://").read())
    transformed, tiers, rep = transform(cleaned)
    print(format_report(rep))

    print("\nDerived columns:")
    for name in rep["derived_columns"]:
        print(f"  {name}")

    print("\n1-minute rollup (first 5 rows, CPU only):")
    cols = [c for c in tiers["1min"].columns if c.startswith("cpu") or c == "ts"]
    print(tiers["1min"][cols].head().to_string(index=False))

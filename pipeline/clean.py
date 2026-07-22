"""
STAGE 5 — Data Cleaning: deduplication and imputation.

Produces a well-formed, continuous series from raw collector output.
Every step records what it changed, so the report is an audit rather than
an assertion.

    1. normalise        coerce dtypes, drop rows with no timestamp
    2. deduplicate      merge duplicate timestamps by averaging
    3. clip_ranges      force values inside the declared contract range
    4. segment_on_gaps  assign segment_id; a break in collection is a wall
    5. regrid           snap each segment onto a uniform time axis
    6. flag_outliers    rolling Hampel score (median + MAD)
    7. repair_outliers  strategy-driven; flag-only by default
    8. impute           time-linear, inside segments, bounded run length

Why segmentation comes before everything windowed
-------------------------------------------------
This dataset has three breaks in collection, the longest 26.7 minutes.
A positional `.shift(20)` says "the value 60 seconds ago". Across a break
it actually returns a value from half an hour earlier, and the model
learns from a history that never happened. Assigning `segment_id` and
running every window operation inside a `groupby("segment_id")` makes
those rows drop out instead of quietly lying.

Why outliers are flagged rather than deleted
--------------------------------------------
CPU here is driven by a load generator and moves between 0% and 100% in a
few samples. A median filter would read those transitions as noise and
erase them — and they are precisely the events an SLA analysis exists to
capture. The default strategy marks them and changes nothing.
"""

import numpy as np
import pandas as pd

import config
from pipeline import schema as schema_mod
from pipeline.validate import detect_cadence


# ----------------------------------------------------------------------
# 1. Normalise
# ----------------------------------------------------------------------
def normalise(df):
    """Coerce to the declared types and drop rows with no usable timestamp."""
    out = schema_mod.coerce(df)
    before = len(out)
    out = out[out["ts"].notna()].copy()
    out = out.sort_values("ts").reset_index(drop=True)
    return out, {"rows_dropped_no_timestamp": before - len(out)}


# ----------------------------------------------------------------------
# 2. Deduplicate
# ----------------------------------------------------------------------
def deduplicate(df):
    """Collapse duplicate timestamps into one row by averaging.

    Two samples claiming the same instant is a collector fault, not a
    signal. Averaging keeps the row rather than arbitrarily choosing one.
    """
    n_dupes = int(df["ts"].duplicated().sum())
    if n_dupes == 0:
        return df, {"duplicate_timestamps_merged": 0}

    numeric = [c for c in schema_mod.value_columns() if c in df.columns]
    merged = df.groupby("ts", as_index=False)[numeric].mean()
    merged = merged.sort_values("ts").reset_index(drop=True)
    return merged, {"duplicate_timestamps_merged": n_dupes}


# ----------------------------------------------------------------------
# 3. Clip to the declared range
# ----------------------------------------------------------------------
def clip_ranges(df):
    """Force values into the range the contract declares.

    A CPU reading of 100.4% is a rounding artefact, not a discovery. The
    count of what was clipped is reported so a systematic problem is
    visible rather than silently corrected.
    """
    out = df.copy()
    clipped = {}

    for rule in schema_mod.contract():
        col = rule["column"]
        if col not in out.columns or rule["min_value"] is None:
            continue
        series = out[col]
        low = int((series < rule["min_value"]).sum())
        high = (int((series > rule["max_value"]).sum())
                if rule["max_value"] is not None else 0)
        if low or high:
            clipped[col] = low + high
        out[col] = series.clip(lower=rule["min_value"], upper=rule["max_value"])

    return out, {"values_clipped": clipped, "values_clipped_total": sum(clipped.values())}


# ----------------------------------------------------------------------
# 4. Segment on collection gaps
# ----------------------------------------------------------------------
def segment_on_gaps(df, cadence=None):
    """Assign `segment_id`, incrementing at every break in collection.

    This is the single most important step in the file. Downstream, every
    lag, rolling window and difference runs inside a groupby on this
    column, so no feature can span a period when the collector was down.
    """
    out = df.copy()
    cadence = cadence or detect_cadence(out)
    threshold = cadence * config.get_float("pipeline.gap_multiple")

    deltas = out["ts"].diff().dt.total_seconds()
    is_break = (deltas > threshold).fillna(False)
    out["segment_id"] = is_break.cumsum().astype(int)

    sizes = out.groupby("segment_id").size()
    return out, {
        "segments": int(out["segment_id"].nunique()),
        "gaps_found": int(is_break.sum()),
        "largest_segment_rows": int(sizes.max()) if len(sizes) else 0,
        "smallest_segment_rows": int(sizes.min()) if len(sizes) else 0,
        "cadence_seconds": round(cadence, 2),
    }


def segment_profile(df):
    """Per-segment rows, span and NATIVE cadence.

    Reported because a segment collected at a different interval is not
    visible in any aggregate statistic, but it changes what a lag depth
    means. This dataset has one such segment.
    """
    rows = []
    for segment_id, group in df.groupby("segment_id"):
        deltas = group["ts"].diff().dt.total_seconds().dropna()
        span = (group["ts"].max() - group["ts"].min()).total_seconds()
        rows.append({
            "segment_id": int(segment_id),
            "rows": len(group),
            "start": group["ts"].min(),
            "span_minutes": round(span / 60.0, 1),
            "native_cadence_s": round(float(deltas.mode().iloc[0]), 1)
                                if not deltas.empty else None,
        })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# 5. Regrid onto a uniform axis
# ----------------------------------------------------------------------
def regrid(df, cadence=None):
    """Snap each segment onto an evenly spaced time axis.

    Lag features are positional: `lag_20` means "twenty rows back" and is
    only "sixty seconds back" if the rows are evenly spaced. This data is
    not evenly spaced — the collector ran at 5s before its interval was
    changed to 3s, so the series carries a mixture. Without regridding,
    the same lag depth means different wall-clock horizons in different
    parts of the series.

    Bucketing, not matching. An earlier version built a `date_range` and
    reindexed onto it, which silently discarded every observation that did
    not land exactly on a grid point — a third of the dataset. Resampling
    assigns each observation to the slot it falls in, so nothing is lost;
    slots with no observation become holes for the imputation step, and
    are marked `is_imputed` so no synthetic value is ever mistaken for a
    measurement.
    """
    cadence = float(cadence or detect_cadence(df))
    if "segment_id" not in df.columns:
        df, _ = segment_on_gaps(df, cadence)

    numeric = [c for c in schema_mod.value_columns() if c in df.columns]
    # Millisecond string rather than a Timedelta built from a numpy float:
    # the latter goes through a generic-unit conversion numpy now warns on.
    freq = f"{int(round(cadence * 1000))}ms"
    pieces, synthetic, collided = [], 0, 0

    for segment_id, group in df.groupby("segment_id"):
        group = group.sort_values("ts").set_index("ts")

        resampler = group[numeric].resample(freq, origin="start")
        regridded = resampler.mean()
        occupancy = resampler.size()

        # Empty slot -> a hole to impute. Two observations in one slot ->
        # they were closer together than the cadence and got averaged.
        synthetic += int((occupancy == 0).sum())
        collided += int((occupancy > 1).sum())

        regridded["segment_id"] = segment_id
        regridded["is_imputed"] = (occupancy == 0).astype(int).values
        regridded = regridded.rename_axis("ts").reset_index()
        pieces.append(regridded)

    out = pd.concat(pieces, ignore_index=True).sort_values("ts").reset_index(drop=True)
    if "id" in out.columns:
        out = out.drop(columns=["id"])

    return out, {
        "synthetic_rows_added": synthetic,
        "observations_merged_into_slot": collided,
        "rows_after_regrid": len(out),
    }


# ----------------------------------------------------------------------
# 6. Flag outliers (rolling Hampel)
# ----------------------------------------------------------------------
def _hampel_scores(series, window, threshold):
    """Modified z-score against a rolling median. Robust to the outliers
    it is looking for, which a mean-and-stdev rule is not."""
    median = series.rolling(window, center=True, min_periods=3).median()
    deviation = (series - median).abs()
    mad = deviation.rolling(window, center=True, min_periods=3).median()
    # 0.6745 rescales MAD to be comparable with a standard deviation.
    scores = 0.6745 * (series - median) / mad.replace(0, np.nan)
    return scores.abs() > threshold, median


def flag_outliers(df):
    """Mark statistically extreme points. Does not alter any value."""
    out = df.copy()
    window = config.get_int("pipeline.outlier_window")
    threshold = config.get_float("pipeline.outlier_mad_threshold")

    flags = pd.Series(False, index=out.index)
    per_column = {}

    for col in schema_mod.target_columns():
        if col not in out.columns:
            continue
        column_flags = pd.Series(False, index=out.index)
        for _, group in out.groupby("segment_id"):
            is_out, _ = _hampel_scores(group[col], window, threshold)
            column_flags.loc[group.index] = is_out.fillna(False)
        out[f"{col}_is_outlier"] = column_flags.astype(int)
        per_column[col] = int(column_flags.sum())
        flags |= column_flags

    out["is_outlier"] = flags.astype(int)
    return out, {"outliers_flagged": per_column,
                 "outlier_rows": int(flags.sum())}


# ----------------------------------------------------------------------
# 7. Repair outliers
# ----------------------------------------------------------------------
def repair_outliers(df, strategy=None):
    """Act on the flags. Default `flag_only` deliberately changes nothing."""
    strategy = strategy or config.get_str("pipeline.outlier_strategy")
    out = df.copy()

    if strategy == "flag_only":
        return out, {"outlier_strategy": strategy, "values_repaired": 0}

    window = config.get_int("pipeline.outlier_window")
    threshold = config.get_float("pipeline.outlier_mad_threshold")
    repaired = 0

    for col in schema_mod.target_columns():
        flag_col = f"{col}_is_outlier"
        if col not in out.columns or flag_col not in out.columns:
            continue
        mask = out[flag_col] == 1
        if not mask.any():
            continue

        if strategy == "interpolate":
            out.loc[mask, col] = np.nan
            out[col] = out.groupby("segment_id")[col].transform(
                lambda s: s.interpolate(limit_direction="both")
            )
        elif strategy == "clip":
            for _, group in out.groupby("segment_id"):
                _, median = _hampel_scores(group[col], window, threshold)
                local = mask.loc[group.index]
                out.loc[group.index[local], col] = median[local].values
        repaired += int(mask.sum())

    return out, {"outlier_strategy": strategy, "values_repaired": repaired}


# ----------------------------------------------------------------------
# 8. Impute
# ----------------------------------------------------------------------
def impute(df):
    """Time-linear interpolation inside segments, bounded run length.

    Beyond `max_interpolate_run` consecutive missing samples the hole is
    left in place. A long invented stretch would be indistinguishable from
    measurement in every plot and metric downstream, which is worse than
    a visible hole.
    """
    out = df.copy()
    limit = config.get_int("pipeline.max_interpolate_run")
    numeric = [c for c in schema_mod.value_columns() if c in out.columns]

    before = {c: int(out[c].isna().sum()) for c in numeric}

    for col in numeric:
        out[col] = out.groupby("segment_id")[col].transform(
            lambda s: s.interpolate(method="linear", limit=limit,
                                    limit_direction="both", limit_area="inside")
        )

    after = {c: int(out[c].isna().sum()) for c in numeric}
    filled = {c: before[c] - after[c] for c in numeric if before[c] - after[c] > 0}

    remaining = sum(after.values())
    if remaining:
        before_drop = len(out)
        out = out.dropna(subset=schema_mod.target_columns()).reset_index(drop=True)
        dropped = before_drop - len(out)
    else:
        dropped = 0

    return out, {
        "values_imputed": filled,
        "values_imputed_total": sum(filled.values()),
        "rows_dropped_unfillable": dropped,
    }


# ----------------------------------------------------------------------
# The whole stage
# ----------------------------------------------------------------------
def clean(df, cadence=None):
    """Run every cleaning step in order. Returns (frame, report)."""
    report = {"rows_in": len(df)}

    df, r = normalise(df);                 report.update(r)
    df, r = deduplicate(df);               report.update(r)
    df, r = clip_ranges(df);               report.update(r)

    cadence = cadence or detect_cadence(df)
    df, r = segment_on_gaps(df, cadence);  report.update(r)
    df, r = regrid(df, cadence);           report.update(r)
    df, r = flag_outliers(df);             report.update(r)
    df, r = repair_outliers(df);           report.update(r)
    df, r = impute(df);                    report.update(r)

    report["rows_out"] = len(df)
    return df, report


def format_report(report):
    lines = [
        "=" * 78,
        "DATA CLEANING",
        "=" * 78,
        f"  rows in                 : {report['rows_in']}",
        f"  dropped, no timestamp   : {report['rows_dropped_no_timestamp']}",
        f"  duplicate ts merged     : {report['duplicate_timestamps_merged']}",
        f"  values clipped to range : {report['values_clipped_total']}",
        f"  cadence measured        : {report['cadence_seconds']}s",
        f"  segments (gap-separated): {report['segments']} "
        f"from {report['gaps_found']} gap(s)",
        f"    largest / smallest    : {report['largest_segment_rows']} / "
        f"{report['smallest_segment_rows']} rows",
        f"  synthetic rows (regrid) : {report['synthetic_rows_added']} "
        f"(marked is_imputed, never mistaken for measurement)",
        f"  obs merged into one slot: {report['observations_merged_into_slot']}",
        f"  outlier rows flagged    : {report['outlier_rows']} "
        f"{report['outliers_flagged']}",
        f"  outlier strategy        : {report['outlier_strategy']} "
        f"({report['values_repaired']} value(s) altered)",
        f"  values imputed          : {report['values_imputed_total']} "
        f"{report['values_imputed'] or ''}",
        f"  dropped, unfillable     : {report['rows_dropped_unfillable']}",
        f"  rows out                : {report['rows_out']}",
        "=" * 78,
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    from pipeline.sources import get_source

    raw = get_source("sqlite://").read()

    staged, _ = normalise(raw)
    staged, _ = segment_on_gaps(staged)
    print("Segments as collected (BEFORE regrid):")
    print(segment_profile(staged).to_string(index=False))

    cleaned, rep = clean(raw)
    print()
    print(format_report(rep))
    print("\nSegments after cleaning:")
    print(segment_profile(cleaned).to_string(index=False))

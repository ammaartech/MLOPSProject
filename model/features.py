"""
STAGE 7 — Feature Engineering: derived, aggregated, date and interaction
features.

Reads the cleaned series produced by the ETL (`metrics_clean`), never the
raw table. Two things distinguish this from the version it replaces.

1. Every window operation is gap-aware
--------------------------------------
Lags, rolling statistics and differences run inside a
`groupby("segment_id")`. The dataset contains three breaks in collection,
the longest 26.7 minutes. A positional `.shift(20)` means "the value 60
seconds ago" only if the rows are contiguous; across a break it returns a
value from half an hour earlier and presents it as recent history. Rows
whose window would straddle a break are dropped instead. `build_features`
reports how many rows a gap-blind build would have kept, so the size of
the contamination is visible rather than assumed.

2. The lag list starts at 0
---------------------------
The previous feature set began at `lag_1`, the PREVIOUS sample, while the
target was `shift(-1)`, the NEXT sample. Predicting y[t+1] from y[t-1]
skips over y[t] — a two-step problem labelled as one step. The naive
baseline was computed the same way, so "next = value from two steps ago"
was the thing being beaten by 15%.

`lag_0` is the current value, which the system genuinely knows at
prediction time. Including it makes this a true one-step forecast and
makes the baseline much harder to beat, because at three-second spacing
"next = current" is already a strong predictor. That is the honest
comparison, and `model/baseline.py` now measures against it.

Train/serve consistency
-----------------------
`build_serving_row()` runs the identical construction as
`build_features()` and returns the final row. There is no second code
path that could drift out of step with the first.
"""

import numpy as np
import pandas as pd

import config
from pipeline import schema as schema_mod
from pipeline import transform as transform_mod
from pipeline.etl import read_clean


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------
def load_clean_frame(run_id=None):
    """The cleaned series from the latest successful ETL run."""
    df = read_clean(run_id)
    if df.empty:
        return df
    return df.sort_values("ts").reset_index(drop=True)


def targets():
    return config.get_json("features.targets")


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------
def _add_lags(data, target, lags):
    """Lagged values of the target, per segment. Lag 0 is the current value."""
    names = []
    grouped = data.groupby("segment_id")[target]
    for lag in lags:
        name = f"{target}_lag_{lag}"
        data[name] = data[target] if lag == 0 else grouped.shift(lag)
        names.append(name)
    return names


def _add_rolling(data, target, window):
    """Rolling summary of recent history, per segment.

    `closed="left"` would exclude the current sample; it is deliberately
    included, because at prediction time the current sample is known and
    excluding it would discard real information.
    """
    names = []
    grouped = data.groupby("segment_id")[target]
    for stat in ("mean", "std", "max", "min"):
        name = f"{target}_roll_{stat}"
        data[name] = grouped.transform(
            lambda s, stat=stat: getattr(s.rolling(window, min_periods=2), stat)()
        )
        names.append(name)
    return names


def _add_calendar(data):
    """Clock features.

    Kept ablatable. On an hour-long dataset driven by a periodic load
    generator, `minute` can memorise the generator's phase and inflate a
    test score without any real forecasting ability. `model/selection.py`
    measures whether it is earning its place rather than assuming it.
    """
    data["hour"] = data["ts"].dt.hour
    data["minute"] = data["ts"].dt.minute
    data["second_of_minute"] = data["ts"].dt.second
    data["dayofweek"] = data["ts"].dt.dayofweek
    return ["hour", "minute", "second_of_minute", "dayofweek"]


def _add_interactions(data, target, window):
    """Products and ratios of history-only quantities.

    Every term below is computable from the current sample and its past,
    so none of them leak. They encode relationships a tree has to spend
    many splits to approximate: level scaled by volatility, how far the
    resource is from saturation relative to how fast it is moving, and
    the current state of the other two resources.
    """
    names = []
    level = data[target]
    volatility = data[f"{target}_roll_std"].fillna(0.0)
    slope = data.groupby("segment_id")[target].diff().fillna(0.0)

    data[f"{target}_level_x_vol"] = level * volatility
    data[f"{target}_slope_x_vol"] = slope * volatility
    names += [f"{target}_level_x_vol", f"{target}_slope_x_vol"]

    # How many samples of headroom remain at the current rate of climb.
    # Large when safe, small when about to saturate; the floor on the
    # denominator keeps a flat series from producing infinities.
    headroom = 100.0 - level
    data[f"{target}_steps_to_saturation"] = headroom / slope.clip(lower=0.05)
    names.append(f"{target}_steps_to_saturation")

    # Deviation from the recent mean, in units of recent volatility.
    data[f"{target}_zscore_local"] = (
        (level - data[f"{target}_roll_mean"]) / volatility.replace(0, np.nan)
    ).fillna(0.0)
    names.append(f"{target}_zscore_local")

    # The other resources' current values, for any cross-resource effect.
    for companion in targets():
        if companion == target or companion not in data.columns:
            continue
        data[f"{companion}_now"] = data[companion]
        names.append(f"{companion}_now")

    return names


def build_feature_frame(df, target):
    """Attach every feature column and the supervised target.

    Returns (frame, feature_columns). The last `target_shift` rows have a
    null target — they are the rows the system must predict, and they are
    what `build_serving_row` returns.
    """
    if df.empty:
        return df, []

    data = df.copy()
    if "segment_id" not in data.columns:
        data["segment_id"] = 0

    lags = config.get_json("features.lags")
    window = config.get_int("features.roll_window")
    shift = config.get_int("features.target_shift")

    # Derived columns come from the transform stage, so "delta" and
    # "trend" have exactly one definition shared by the dashboard, the
    # training path and the serving path.
    data, _ = transform_mod.add_derived(data)

    feature_cols = []
    feature_cols += _add_lags(data, target, lags)
    feature_cols += _add_rolling(data, target, window)
    feature_cols += [
        f"{target}_delta", f"{target}_roll_std", f"{target}_ewma_fast",
        f"{target}_ewma_slow", f"{target}_trend", f"{target}_headroom",
    ]

    if config.get_bool("features.use_interactions"):
        feature_cols += _add_interactions(data, target, window)
    if config.get_bool("features.use_calendar"):
        feature_cols += _add_calendar(data)

    # The supervised target: the value `shift` steps into the future,
    # within this segment. Across a break there is no "next value", and
    # the null that produces is correct.
    data["__target__"] = data.groupby("segment_id")[target].shift(-shift)

    # Deduplicate while preserving order — `{target}_roll_std` is produced
    # by both the rolling block and the derived block.
    seen, ordered = set(), []
    for name in feature_cols:
        if name not in seen and name in data.columns:
            seen.add(name)
            ordered.append(name)

    return data, ordered


def build_features(df, target):
    """The supervised training table. Returns (X, y, meta)."""
    data, feature_cols = build_feature_frame(df, target)
    if data.empty or not feature_cols:
        return pd.DataFrame(), pd.Series(dtype=float), {"rows_usable": 0}

    complete = data.dropna(subset=feature_cols + ["__target__"])

    # What a gap-blind build would have kept: the same construction with
    # every segment boundary ignored. The difference is the number of rows
    # whose "recent history" would have come from before a break.
    naive_rows = _gap_blind_row_count(df, target)

    meta = {
        "target": target,
        "feature_columns": feature_cols,
        "n_features": len(feature_cols),
        "rows_usable": len(complete),
        "rows_a_gap_blind_build_would_have_kept": naive_rows,
        "contaminated_rows_excluded": max(0, naive_rows - len(complete)),
        "segments_contributing": int(complete["segment_id"].nunique()),
        "timestamps": complete["ts"].reset_index(drop=True),
        "segment_ids": complete["segment_id"].reset_index(drop=True),
        "regimes": (complete["regime"].reset_index(drop=True)
                    if "regime" in complete.columns else None),
        # The value at prediction time — the naive baseline's prediction.
        "actual_now": complete[f"{target}_lag_0"].reset_index(drop=True),
        "is_imputed": (complete["is_imputed"].reset_index(drop=True)
                       if "is_imputed" in complete.columns else None),
    }

    X = complete[feature_cols].reset_index(drop=True)
    y = complete["__target__"].reset_index(drop=True)
    return X, y, meta


def build_serving_row(df, target):
    """One feature vector for the most recent moment, for inference.

    Identical construction to `build_features`, so a feature can never
    mean one thing in training and another in serving. Returns
    (row_frame, meta) or (None, reason).
    """
    data, feature_cols = build_feature_frame(df, target)
    if data.empty or not feature_cols:
        return None, {"error": "no data"}

    usable = data.dropna(subset=feature_cols)
    if usable.empty:
        return None, {
            "error": "no row has a complete feature vector; "
                     "need more contiguous history since the last gap"
        }

    latest = usable.iloc[[-1]]
    return latest[feature_cols], {
        "ts": latest["ts"].iloc[0],
        "segment_id": int(latest["segment_id"].iloc[0]),
        "current_value": float(latest[f"{target}_lag_0"].iloc[0]),
        "feature_columns": feature_cols,
    }


def _gap_blind_row_count(df, target):
    """Rows the previous, segment-unaware construction would have kept."""
    data = df.copy()
    lags = config.get_json("features.lags")
    window = config.get_int("features.roll_window")
    shift = config.get_int("features.target_shift")

    columns = []
    for lag in lags:
        name = f"_gb_lag_{lag}"
        data[name] = data[target] if lag == 0 else data[target].shift(lag)
        columns.append(name)
    for stat in ("mean", "std", "max", "min"):
        name = f"_gb_roll_{stat}"
        data[name] = getattr(data[target].rolling(window, min_periods=2), stat)()
        columns.append(name)

    data["_gb_target"] = data[target].shift(-shift)
    return int(len(data.dropna(subset=columns + ["_gb_target"])))


# ----------------------------------------------------------------------
# Splits — chronological, never shuffled
# ----------------------------------------------------------------------
def chronological_split(X, y, meta=None, test_frac=None):
    """Train on the earlier portion, test on the later.

    Shuffling a time series puts rows from after the test window into
    training and produces a score that cannot be reproduced in
    production.
    """
    test_frac = test_frac if test_frac is not None else config.get_float("model.test_fraction")
    split = int(len(X) * (1 - test_frac))

    result = {
        "X_train": X.iloc[:split], "X_test": X.iloc[split:],
        "y_train": y.iloc[:split], "y_test": y.iloc[split:],
        "split_index": split,
    }
    if meta:
        for key in ("timestamps", "actual_now", "regimes", "segment_ids", "is_imputed"):
            series = meta.get(key)
            if series is not None:
                result[f"{key}_test"] = series.iloc[split:].reset_index(drop=True)
    return result


def rolling_origin_splits(X, y, n_folds=None, min_train_frac=0.4):
    """Expanding-window cross-validation for time series.

    Each fold trains on everything up to a point and tests on the block
    that follows. A single holdout can be lucky or unlucky depending on
    which part of the load-generator cycle it lands in; this measures the
    spread across several.
    """
    n_folds = n_folds or config.get_int("model.cv_folds")
    n = len(X)
    start = int(n * min_train_frac)
    if n - start < n_folds * 2:
        return []

    fold_size = (n - start) // n_folds
    splits = []
    for fold in range(n_folds):
        train_end = start + fold * fold_size
        test_end = train_end + fold_size if fold < n_folds - 1 else n
        if train_end <= 0 or test_end <= train_end:
            continue
        splits.append({
            "fold": fold,
            "X_train": X.iloc[:train_end], "y_train": y.iloc[:train_end],
            "X_test": X.iloc[train_end:test_end], "y_test": y.iloc[train_end:test_end],
        })
    return splits


# ----------------------------------------------------------------------
def format_report(metas):
    lines = [
        "=" * 84,
        "FEATURE ENGINEERING",
        "=" * 84,
        f"  {'target':14s} {'usable':>7} {'feats':>6} {'gap-blind':>10} "
        f"{'contaminated':>13} {'segments':>9}",
    ]
    for meta in metas:
        lines.append(
            f"  {meta['target']:14s} {meta['rows_usable']:>7} "
            f"{meta['n_features']:>6} "
            f"{meta['rows_a_gap_blind_build_would_have_kept']:>10} "
            f"{meta['contaminated_rows_excluded']:>13} "
            f"{meta['segments_contributing']:>9}"
        )
    lines.append("-" * 84)
    lines.append(
        "  'contaminated' = rows a segment-unaware build would have kept whose\n"
        "  history reaches back across a break in collection."
    )
    lines.append("=" * 84)
    return "\n".join(lines)


if __name__ == "__main__":
    frame = load_clean_frame()
    if frame.empty:
        raise SystemExit("No cleaned data. Run: python -m pipeline.etl")

    print(f"Clean frame: {len(frame)} rows, "
          f"{frame['segment_id'].nunique()} segment(s)\n")

    metas = []
    for tgt in targets():
        X, y, meta = build_features(frame, tgt)
        metas.append(meta)
    print(format_report(metas))

    example = targets()[0]
    X, y, meta = build_features(frame, example)
    print(f"\nFeature columns for {example} ({meta['n_features']}):")
    for name in meta["feature_columns"]:
        print(f"    {name}")

    row, info = build_serving_row(frame, example)
    print(f"\nServing row for {example}: ts={info.get('ts')} "
          f"current={info.get('current_value')}")

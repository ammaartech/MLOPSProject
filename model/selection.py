"""
STAGE 8 — Feature Selection: cardinality, correlation, importance.

Three screens, applied in that order because each is cheaper than the one
after it and removes work from it.

    1. cardinality    a feature with no variance cannot inform a split
    2. correlation    of two near-identical features, keep the useful one
    3. importance     permutation importance at or below noise contributes
                      nothing measurable and costs inference time

Why permutation importance rather than the model's own
------------------------------------------------------
`GradientBoostingRegressor.feature_importances_` counts how often a
feature was split on, weighted by the gain. That measure is biased toward
high-cardinality continuous features and is computed on TRAINING data, so
a feature the model memorised the training set with scores highly even
when it generalises nothing. Permutation importance shuffles one column
in the TEST set and measures how much the error grows — it asks the
question the pipeline actually cares about, which is whether the feature
carries information about unseen data.

Why anything is protected
-------------------------
`{target}_lag_0` is the current value. It is the naive baseline's entire
prediction and the anchor every other feature is relative to. On a series
this autocorrelated it may correlate above 0.95 with several other
features and be a candidate for removal on those grounds — removing it
would be a mistake, so it is held back from the correlation screen and
allowed to compete only on measured importance.
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.metrics import mean_absolute_error

import config
from model.features import build_features, chronological_split


def protected_features(target):
    """Features exempt from the correlation screen. See the module note."""
    return {f"{target}_lag_0"}


# ----------------------------------------------------------------------
# 1. Cardinality / variance
# ----------------------------------------------------------------------
def cardinality_screen(X, min_variance=None):
    """Drop features that are constant or effectively so."""
    min_variance = (min_variance if min_variance is not None
                    else config.get_float("selection.min_variance"))

    dropped = {}
    for col in X.columns:
        series = pd.to_numeric(X[col], errors="coerce")
        variance = float(series.var())
        if not np.isfinite(variance) or variance <= min_variance:
            dropped[col] = (f"variance {variance:.3g} <= {min_variance:.3g} "
                            f"({series.nunique()} distinct value(s))")
    return [c for c in X.columns if c not in dropped], dropped


# ----------------------------------------------------------------------
# 2. Correlation
# ----------------------------------------------------------------------
def correlation_screen(X, importance=None, threshold=None, protected=()):
    """Collapse near-duplicate features.

    When two features exceed the threshold, the one the model relies on
    less is dropped. Without an importance ranking the later column goes,
    which is arbitrary — so the caller passes importances when it has
    them.
    """
    threshold = (threshold if threshold is not None
                 else config.get_float("selection.corr_threshold"))

    correlation = X.corr(numeric_only=True).abs()
    ranking = importance if importance is not None else {}

    dropped, keep = {}, list(X.columns)
    for i, a in enumerate(X.columns):
        if a in dropped:
            continue
        for b in X.columns[i + 1:]:
            if b in dropped:
                continue
            value = correlation.loc[a, b]
            if not np.isfinite(value) or value < threshold:
                continue

            # Prefer keeping the more important of the pair; a protected
            # feature always survives.
            if b in protected and a not in protected:
                loser, winner = a, b
            elif a in protected and b not in protected:
                loser, winner = b, a
            elif ranking.get(a, 0.0) >= ranking.get(b, 0.0):
                loser, winner = b, a
            else:
                loser, winner = a, b

            dropped[loser] = (f"|r| = {value:.4f} with {winner} "
                              f"(>= {threshold}); kept the more important")
            if loser == a:
                break

    return [c for c in keep if c not in dropped], dropped


# ----------------------------------------------------------------------
# 3. Permutation importance
# ----------------------------------------------------------------------
def importance_screen(model, X_test, y_test, min_importance=None, repeats=None,
                      protected=()):
    """Drop features whose shuffling does not measurably hurt the model."""
    min_importance = (min_importance if min_importance is not None
                      else config.get_float("selection.min_importance"))
    repeats = repeats or config.get_int("selection.permutation_repeats")

    result = permutation_importance(
        model, X_test, y_test,
        n_repeats=repeats,
        random_state=config.get_int("model.seed"),
        scoring="neg_mean_absolute_error",
    )

    scores = dict(zip(X_test.columns, result.importances_mean))
    dropped = {
        col: f"permutation importance {score:.6f} <= {min_importance:.6f}"
        for col, score in scores.items()
        if score <= min_importance and col not in protected
    }
    return [c for c in X_test.columns if c not in dropped], dropped, scores


# ----------------------------------------------------------------------
# The stage
# ----------------------------------------------------------------------
def run(df, target):
    """Apply all three screens to one target. Returns a summary dict."""
    X, y, meta = build_features(df, target)
    if X.empty:
        return {"target": target, "error": "no usable feature rows"}

    split = chronological_split(X, y, meta)
    protected = protected_features(target)
    params = config.get_json("model.params")
    dropped_all = {}

    # --- screen 1 -------------------------------------------------------
    kept, dropped = cardinality_screen(X)
    dropped_all.update({k: ("cardinality", v) for k, v in dropped.items()})
    if not kept:
        return {"target": target, "error": "every feature is constant"}

    # --- fit once, to rank the survivors for screens 2 and 3 ------------
    model = GradientBoostingRegressor(**params)
    model.fit(split["X_train"][kept], split["y_train"])
    baseline_mae = mean_absolute_error(
        split["y_test"], model.predict(split["X_test"][kept])
    )

    _, _, scores = importance_screen(
        model, split["X_test"][kept], split["y_test"], protected=protected
    )

    # --- screen 2 -------------------------------------------------------
    kept, dropped = correlation_screen(X[kept], importance=scores,
                                       protected=protected)
    dropped_all.update({k: ("correlation", v) for k, v in dropped.items()})

    # --- screen 3, on the reduced set -----------------------------------
    model = GradientBoostingRegressor(**params)
    model.fit(split["X_train"][kept], split["y_train"])
    kept, dropped, scores = importance_screen(
        model, split["X_test"][kept], split["y_test"], protected=protected
    )
    dropped_all.update({k: ("importance", v) for k, v in dropped.items()})

    # --- did removing them cost anything? -------------------------------
    reduced = GradientBoostingRegressor(**params)
    reduced.fit(split["X_train"][kept], split["y_train"])
    reduced_mae = mean_absolute_error(
        split["y_test"], reduced.predict(split["X_test"][kept])
    )

    table = pd.DataFrame([
        {
            "feature": col,
            "verdict": "kept" if col in kept else "dropped",
            "stage": dropped_all.get(col, ("kept", ""))[0],
            "importance": round(float(scores.get(col, np.nan)), 6)
                          if col in scores else None,
            "reason": ("protected: naive baseline anchor" if col in protected
                       else dropped_all.get(col, ("", "informative"))[1]),
        }
        for col in meta["feature_columns"]
    ]).sort_values(["verdict", "importance"], ascending=[True, False])

    return {
        "target": target,
        "n_before": len(meta["feature_columns"]),
        "n_after": len(kept),
        "selected": kept,
        "dropped": dropped_all,
        "mae_all_features": round(float(baseline_mae), 4),
        "mae_selected": round(float(reduced_mae), 4),
        "mae_change": round(float(reduced_mae - baseline_mae), 4),
        "table": table,
    }


def run_all(df, target_list=None):
    return {
        target: run(df, target)
        for target in (target_list or config.get_json("features.targets"))
    }


def format_report(summaries):
    lines = ["=" * 84, "FEATURE SELECTION", "=" * 84]
    for target, s in summaries.items():
        if "error" in s:
            lines.append(f"\n  {target}: {s['error']}")
            continue
        direction = "worse" if s["mae_change"] > 0 else "better"
        lines.append(
            f"\n  {target}: {s['n_before']} -> {s['n_after']} features"
        )
        lines.append(
            f"    MAE all={s['mae_all_features']}  selected={s['mae_selected']}  "
            f"({s['mae_change']:+.4f}, {direction})"
        )
        by_stage = {}
        for col, (stage, _) in s["dropped"].items():
            by_stage.setdefault(stage, []).append(col)
        for stage, cols in by_stage.items():
            lines.append(f"    dropped by {stage:12s}: {len(cols)} -> "
                         f"{', '.join(cols[:5])}"
                         + (" ..." if len(cols) > 5 else ""))
    lines.append("=" * 84)
    return "\n".join(lines)


if __name__ == "__main__":
    from model.features import load_clean_frame

    frame = load_clean_frame()
    if frame.empty:
        raise SystemExit("No cleaned data. Run: python -m pipeline.etl")

    results = run_all(frame)
    print(format_report(results))

    example = config.get_json("features.targets")[0]
    if "table" in results[example]:
        print(f"\nPer-feature detail for {example}:")
        print(results[example]["table"].to_string(index=False))

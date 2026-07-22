"""
STAGE 9 — Feature Scaling: min-max, standard, robust.

Three methods, all fitted on TRAINING ROWS ONLY.

Why that qualifier is the whole point
-------------------------------------
A scaler fitted on the full dataset has seen the mean and spread of the
test window. Every training row is then expressed in units derived partly
from data the model is about to be evaluated on. The leak is subtle —
scores improve slightly, nothing errors — and it invalidates the
comparison. `fit_scaler` therefore takes the training frame alone, and
`apply_scaler` is the only way to transform anything else.

Why the pipeline still measures whether scaling matters
-------------------------------------------------------
Gradient boosting splits on rank order, so any monotonic rescaling leaves
its predictions unchanged; scaling it is wasted work. Ridge regression
penalises coefficients, and a coefficient's size depends on its feature's
units, so ridge genuinely needs it. Rather than assert this,
`compare_methods()` trains both under all three scalings and reports the
spread. Knowing which parts of a pipeline do nothing is worth as much as
knowing which parts help.
"""

import numpy as np
import pandas as pd

import config

METHODS = ("standard", "minmax", "robust")


# ----------------------------------------------------------------------
# Fit / apply
# ----------------------------------------------------------------------
def fit_scaler(train_df, columns=None, method=None):
    """Fit a scaler on training rows. Returns a serialisable dict.

    A plain dict rather than a sklearn object so the parameters can be
    written into the model registry and inspected later — a scaler whose
    centre has drifted is itself a signal.
    """
    method = method or config.get_str("scaling.method")
    if method not in METHODS:
        raise ValueError(f"unknown scaling method '{method}'; expected {METHODS}")

    columns = columns or [
        c for c in train_df.columns
        if pd.api.types.is_numeric_dtype(train_df[c])
    ]

    centre, spread = {}, {}
    for col in columns:
        series = pd.to_numeric(train_df[col], errors="coerce")

        if method == "standard":
            centre[col] = float(series.mean())
            spread[col] = float(series.std())
        elif method == "minmax":
            centre[col] = float(series.min())
            spread[col] = float(series.max() - series.min())
        else:  # robust — median and IQR, unmoved by the outliers we flagged
            centre[col] = float(series.median())
            spread[col] = float(series.quantile(0.75) - series.quantile(0.25))

        # A constant feature has zero spread. Dividing by it produces inf;
        # a spread of 1 leaves it at a constant offset, which is harmless
        # and lets the selection stage drop it on its own terms.
        if not np.isfinite(spread[col]) or abs(spread[col]) < 1e-12:
            spread[col] = 1.0

    return {
        "method": method,
        "columns": list(columns),
        "centre": centre,
        "spread": spread,
        "fitted_on_rows": len(train_df),
    }


def apply_scaler(df, scaler):
    """Transform a frame with an already-fitted scaler."""
    out = df.copy()
    for col in scaler["columns"]:
        if col not in out.columns:
            continue
        out[col] = (pd.to_numeric(out[col], errors="coerce")
                    - scaler["centre"][col]) / scaler["spread"][col]
    return out


def fit_apply(X_train, X_test, method=None):
    """Fit on train, transform both. The only correct order."""
    scaler = fit_scaler(X_train, method=method)
    return apply_scaler(X_train, scaler), apply_scaler(X_test, scaler), scaler


def describe(scaler):
    rows = [
        {
            "feature": col,
            "centre": round(scaler["centre"][col], 4),
            "spread": round(scaler["spread"][col], 4),
        }
        for col in scaler["columns"]
    ]
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# Does scaling actually change anything here?
# ----------------------------------------------------------------------
def compare_methods(split, ridge_alpha=None):
    """Train GBR and Ridge under every scaling and report the MAE spread."""
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.linear_model import Ridge
    from sklearn.metrics import mean_absolute_error

    ridge_alpha = ridge_alpha or config.get_float("model.ridge_alpha")
    params = config.get_json("model.params")
    X_train, X_test = split["X_train"], split["X_test"]
    y_train, y_test = split["y_train"], split["y_test"]

    rows = []
    for method in ("none",) + METHODS:
        if method == "none":
            train_frame, test_frame = X_train, X_test
        else:
            train_frame, test_frame, _ = fit_apply(X_train, X_test, method)

        gbr = GradientBoostingRegressor(**params).fit(train_frame, y_train)
        ridge = Ridge(alpha=ridge_alpha).fit(train_frame, y_train)

        rows.append({
            "scaling": method,
            "gbr_mae": round(mean_absolute_error(y_test, gbr.predict(test_frame)), 4),
            "ridge_mae": round(mean_absolute_error(y_test, ridge.predict(test_frame)), 4),
        })

    return pd.DataFrame(rows)


def invariance_verdicts(table):
    """Turn the spread into a statement about each estimator."""
    verdicts = {}
    for estimator, reason in (
        ("gbr", "splits on rank order, so monotonic rescaling cannot change it"),
        ("ridge", "the penalty is scale-sensitive"),
    ):
        column = f"{estimator}_mae"
        spread = float(table[column].max() - table[column].min())
        best = table.loc[table[column].idxmin(), "scaling"]
        verdicts[estimator] = {
            "spread": round(spread, 6),
            "invariant": spread < 0.01,
            "best_method": best,
            "reason": reason,
        }
    return verdicts


def format_report(table, verdicts):
    lines = [
        "=" * 76,
        "FEATURE SCALING",
        "=" * 76,
        "  " + table.to_string(index=False).replace("\n", "\n  "),
        "-" * 76,
    ]
    for estimator, v in verdicts.items():
        status = "INVARIANT" if v["invariant"] else "SCALE-SENSITIVE"
        detail = ("MAE spread is float noise" if v["invariant"]
                  else f"best = {v['best_method']}")
        lines.append(f"  {estimator:6s}: {status:16s} ({v['reason']}) "
                     f"-> spread {v['spread']}, {detail}")
    lines.append("=" * 76)
    return "\n".join(lines)


if __name__ == "__main__":
    from model.features import (
        build_features, chronological_split, load_clean_frame, targets,
    )

    frame = load_clean_frame()
    if frame.empty:
        raise SystemExit("No cleaned data. Run: python -m pipeline.etl")

    target = targets()[0]
    X, y, meta = build_features(frame, target)
    split = chronological_split(X, y, meta)

    print(f"Target: {target}  (train {len(split['X_train'])} / "
          f"test {len(split['X_test'])} rows)\n")

    scaler = fit_scaler(split["X_train"])
    print(f"Fitted '{scaler['method']}' scaler on "
          f"{scaler['fitted_on_rows']} TRAINING rows only\n")
    print(describe(scaler).head(8).to_string(index=False))

    print()
    comparison = compare_methods(split)
    print(format_report(comparison, invariance_verdicts(comparison)))

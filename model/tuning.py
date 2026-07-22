"""
STAGE 11 (tuning) — hyperparameter search.

Randomised search over the grid in `tuning.grid`, scored by ROLLING-ORIGIN
cross-validation rather than shuffled K-fold.

Why not GridSearchCV
--------------------
`GridSearchCV` and `RandomizedSearchCV` default to KFold, which assigns
rows to folds at random. On a time series that trains on data from after
the validation window. The resulting score is optimistic and unreachable
in production, and nothing warns you. Every candidate here is scored with
expanding-window splits from `model.features.rolling_origin_splits`, so
each fold trains only on the past.

Why the search budget is small
------------------------------
The grid has 960 combinations; the default budget is 30. With 247 usable
rows and a four-fold CV, an exhaustive search would fit thousands of
models to pick between differences smaller than the fold-to-fold spread.
The CV standard deviation is reported next to every score so it stays
visible whether a "better" candidate is actually distinguishable from
the incumbent.
"""

import random

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error

import config
from model.features import (
    build_features, chronological_split, load_clean_frame, rolling_origin_splits,
)


def sample_grid(grid=None, n_iter=None, seed=None):
    """Draw `n_iter` distinct candidates from the search space."""
    grid = grid or config.get_json("tuning.grid")
    n_iter = n_iter or config.get_int("tuning.n_iter")
    seed = seed if seed is not None else config.get_int("model.seed")

    rng = random.Random(seed)
    keys = sorted(grid)
    seen, candidates = set(), []

    # Bounded attempts: once the space is mostly explored, resampling
    # duplicates forever would hang rather than fail.
    for _ in range(n_iter * 20):
        if len(candidates) >= n_iter:
            break
        candidate = {key: rng.choice(grid[key]) for key in keys}
        signature = tuple(sorted(candidate.items()))
        if signature in seen:
            continue
        seen.add(signature)
        candidates.append(candidate)

    return candidates


def cv_score(X, y, params, n_folds=None):
    """Rolling-origin CV score for one candidate. Returns (mean, std)."""
    folds = rolling_origin_splits(X, y, n_folds=n_folds)
    if not folds:
        return float("inf"), 0.0

    scores = []
    for fold in folds:
        model = GradientBoostingRegressor(
            random_state=config.get_int("model.seed"), **params
        )
        model.fit(fold["X_train"], fold["y_train"])
        scores.append(float(mean_absolute_error(
            fold["y_test"], model.predict(fold["X_test"])
        )))
    return float(np.mean(scores)), float(np.std(scores))


def tune(df, target, n_iter=None, verbose=False):
    """Search for the best hyperparameters for one target."""
    X, y, _meta = build_features(df, target)
    if len(X) < 40:
        return {"target": target, "error": f"only {len(X)} rows; too few to tune"}

    incumbent = {k: v for k, v in config.get_json("model.params").items()
                 if k != "random_state"}
    incumbent_mean, incumbent_std = cv_score(X, y, incumbent)

    candidates = sample_grid(n_iter=n_iter)
    rows = []
    for index, candidate in enumerate(candidates):
        mean, std = cv_score(X, y, candidate)
        rows.append({"trial": index, **candidate,
                     "cv_mae": round(mean, 5), "cv_std": round(std, 5)})
        if verbose:
            print(f"    trial {index:>2}: {mean:.5f} +/- {std:.5f}  {candidate}")

    table = pd.DataFrame(rows).sort_values("cv_mae").reset_index(drop=True)
    best_row = table.iloc[0]

    # Index back into the original dicts rather than reading the row.
    # A DataFrame column holding both ints and floats promotes everything
    # to float64, and sklearn rejects `max_depth=2.0` as not an integer.
    best = candidates[int(best_row["trial"])]

    # Held-out confirmation: CV picked it, the test window judges it.
    split = chronological_split(X, y)
    tuned_model = GradientBoostingRegressor(
        random_state=config.get_int("model.seed"), **best
    ).fit(split["X_train"], split["y_train"])
    tuned_test_mae = float(mean_absolute_error(
        split["y_test"], tuned_model.predict(split["X_test"])
    ))

    incumbent_model = GradientBoostingRegressor(
        random_state=config.get_int("model.seed"), **incumbent
    ).fit(split["X_train"], split["y_train"])
    incumbent_test_mae = float(mean_absolute_error(
        split["y_test"], incumbent_model.predict(split["X_test"])
    ))

    gain = incumbent_mean - float(best_row["cv_mae"])
    distinguishable = gain > float(best_row["cv_std"])

    return {
        "target": target,
        "trials": len(table),
        "best_params": {k: _native(v) for k, v in best.items()},
        "best_cv_mae": float(best_row["cv_mae"]),
        "best_cv_std": float(best_row["cv_std"]),
        "incumbent_params": incumbent,
        "incumbent_cv_mae": round(incumbent_mean, 5),
        "incumbent_cv_std": round(incumbent_std, 5),
        "cv_gain": round(gain, 5),
        "distinguishable_from_noise": bool(distinguishable),
        "tuned_test_mae": round(tuned_test_mae, 5),
        "incumbent_test_mae": round(incumbent_test_mae, 5),
        "test_gain": round(incumbent_test_mae - tuned_test_mae, 5),
        "table": table,
    }


def run_all(df=None, n_iter=None):
    if df is None:
        df = load_clean_frame()
    return {
        target: tune(df, target, n_iter=n_iter)
        for target in config.get_json("features.targets")
    }


def apply_best(summary):
    """Write tuned hyperparameters into the config table.

    Only when the gain exceeds the fold-to-fold spread. Adopting a
    difference smaller than the noise is how a pipeline slowly overfits
    its own validation procedure.
    """
    if "error" in summary or not summary["distinguishable_from_noise"]:
        return {"applied": False,
                "reason": summary.get("error",
                                      "gain is within the CV spread; "
                                      "not distinguishable from noise")}

    params = dict(summary["best_params"])
    params["random_state"] = config.get_int("model.seed")
    config.set_value("model.params", params,
                     source=f"tuning: cv {summary['incumbent_cv_mae']} -> "
                            f"{summary['best_cv_mae']} on {summary['target']}")
    return {"applied": True, "params": params}


def _native(value):
    return value.item() if hasattr(value, "item") else value


def format_report(summaries):
    lines = ["=" * 88, "HYPERPARAMETER TUNING (rolling-origin CV)", "=" * 88]
    for target, s in summaries.items():
        if "error" in s:
            lines.append(f"\n  {target}: {s['error']}")
            continue
        lines += [
            f"\n  {target}  ({s['trials']} trials)",
            f"    incumbent  : {s['incumbent_cv_mae']} +/- {s['incumbent_cv_std']} "
            f"(CV)   test {s['incumbent_test_mae']}",
            f"    best found : {s['best_cv_mae']} +/- {s['best_cv_std']} "
            f"(CV)   test {s['tuned_test_mae']}",
            f"    CV gain    : {s['cv_gain']:+.5f}  "
            f"-> {'distinguishable from noise' if s['distinguishable_from_noise'] else 'WITHIN the fold spread; not adopted'}",
            f"    params     : {s['best_params']}",
        ]
    lines.append("\n" + "=" * 88)
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    frame = load_clean_frame()
    if frame.empty:
        raise SystemExit("No cleaned data. Run: python -m pipeline.etl")

    budget = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    print(f"Searching {budget} candidates per target with rolling-origin CV...\n")

    results = run_all(frame, n_iter=budget)
    print(format_report(results))

    target = config.get_json("features.targets")[0]
    if "table" in results[target]:
        print(f"\nTop candidates for {target}:")
        print(results[target]["table"].head(6).to_string(index=False))

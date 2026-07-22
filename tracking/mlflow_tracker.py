"""
Experiment tracking and the promotion gate.

Every training run is logged to MLflow with its parameters, metrics and
the lineage identifiers that tie it to specific data. Separately — and
this is the part that matters — a gate decides whether the model just
trained is allowed to serve traffic.

The gate
--------
A challenger is promoted only if it clears both tests:

    1. It beats the strongest BASELINE on the same test window.
       `promotion.require_beat_baseline`
    2. It beats the incumbent CHAMPION by at least
       `promotion.min_improvement_pct`, which stops the champion churning
       on noise.

The first test is the one with teeth here. The gradient booster loses to
persistence on CPU, so it is REJECTED and never reaches production. That
is the gate working, not the gate failing: its purpose is to keep a model
that is worse than a trivial rule from being deployed because someone
trained it.

When no model passes, the system serves a BASELINE predictor instead.
`promote_baseline()` records persistence as the champion, so production
has a defined, measured predictor rather than an empty registry — and the
model registry states plainly that the ML model did not earn its place.

MLflow 3.x notes
----------------
* The file-based `./mlruns` backend is gone. A SQLite backend URI is
  required; most tutorials online are stale on this.
* `log_model(model, name=...)`. `artifact_path=` is deprecated. There is
  a fallback below for 2.x.
"""

import json
from datetime import datetime

import config
from crud.query import execute_query

EXPERIMENT = "predictive-resource-monitoring"


# ----------------------------------------------------------------------
# MLflow plumbing
# ----------------------------------------------------------------------
def init_mlflow():
    """Point MLflow at the SQLite backend and select the experiment."""
    import mlflow

    mlflow.set_tracking_uri(config.MLFLOW_URI)
    mlflow.set_experiment(EXPERIMENT)
    return mlflow


def log_training_run(result, register_model=True):
    """Log one training result to MLflow. Returns the mlflow run id."""
    if "error" in result:
        return None

    mlflow = init_mlflow()

    with mlflow.start_run(run_name=result["model_id"]) as run:
        mlflow.log_params({
            "target": result["target"],
            "algorithm": result["algorithm"],
            "n_features": result["n_features"],
            "train_rows": result["train_rows"],
            "test_rows": result["test_rows"],
            **{f"hp_{k}": v for k, v in result["params"].items()},
        })

        # Lineage as tags: what data, what feature definition, what config.
        mlflow.set_tags({
            "model_id": result["model_id"],
            "feature_version": result["feature_version"] or "",
            "data_fingerprint": result["data_fingerprint"] or "",
            "config_fingerprint": result["config_fingerprint"] or "",
            "etl_run_id": str(result["run_id"]),
            "flat_signal": str(result["flat_signal"]),
        })

        metrics = {
            "mae": result["mae"],
            "rmse": result["rmse"],
            "inference_ms": result["inference_ms"],
        }
        for key in ("baseline_mae", "persistence_mae", "old_baseline_mae",
                    "cv_mae_mean", "cv_mae_std", "improvement_pct",
                    "improvement_vs_old_baseline_pct"):
            if result.get(key) is not None:
                metrics[key] = result[key]
        mlflow.log_metrics(metrics)

        # The full ladder as an artifact — the comparison is the evidence.
        mlflow.log_text(result["ladder"].to_string(index=False),
                        "baseline_ladder.txt")

        if register_model:
            try:
                # MLflow 3.x signature.
                mlflow.sklearn.log_model(result.pop("_estimator", None)
                                         or _load_estimator(result),
                                         name="model")
            except TypeError:
                # 2.x fallback.
                mlflow.sklearn.log_model(_load_estimator(result),
                                         artifact_path="model")
            except Exception as exc:                       # noqa: BLE001
                mlflow.set_tag("model_logging_error", str(exc))

        mlflow_run_id = run.info.run_id

    execute_query(
        "UPDATE model_versions SET mlflow_run_id = ? WHERE model_id = ?",
        (mlflow_run_id, result["model_id"]),
    )
    return mlflow_run_id


def _load_estimator(result):
    from model.forecast import load_model

    bundle = load_model(result["model_id"])
    return bundle["model"] if bundle else None


# ----------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------
def current_champion(target):
    rows = execute_query(
        """
        SELECT model_id, algorithm, mae, baseline_mae, feature_version, promoted_at
        FROM model_versions
        WHERE target = ? AND is_champion = 1
        ORDER BY promoted_at DESC LIMIT 1
        """,
        (target,), fetch=True,
    )
    if not rows:
        return None
    r = rows[0]
    return {"model_id": r[0], "algorithm": r[1], "mae": r[2],
            "baseline_mae": r[3], "feature_version": r[4], "promoted_at": r[5]}


def evaluate(result):
    """Decide whether `result` should be promoted. Returns a decision dict."""
    if "error" in result:
        return {"promote": False, "verdict": "ERROR", "reason": result["error"]}

    target = result["target"]
    incumbent = current_champion(target)
    require_baseline = config.get_bool("promotion.require_beat_baseline")
    min_improvement = config.get_float("promotion.min_improvement_pct")

    # --- test 1: does it beat the strongest simple alternative? ---------
    baseline_mae = result.get("baseline_mae")
    if require_baseline and baseline_mae is not None:
        if result["mae"] >= baseline_mae:
            # A constant series gives the baseline MAE 0.0 — a perfect
            # score. Any percentage against it is undefined, so the margin
            # is stated in absolute terms instead.
            if baseline_mae == 0:
                margin = f"baseline is exact (MAE 0.0); the model adds {result['mae']} error"
            else:
                pct = (result["mae"] - baseline_mae) / baseline_mae * 100
                margin = f"{result['mae']} vs {baseline_mae}, {pct:+.1f}%"

            return {
                "promote": False,
                "verdict": "REJECTED",
                "reason": (
                    f"loses to the '{result['baseline_name']}' baseline "
                    f"({margin}). A model worse than a trivial rule must "
                    f"not serve traffic."
                ),
                "incumbent": incumbent,
            }

    # --- test 1b: does the win survive cross-validation? -----------------
    # A holdout is one window. This checks the same comparison across
    # rolling-origin folds, on the grounds that a model which wins once
    # and loses elsewhere is not better — it was lucky.
    won_fraction = result.get("cv_folds_won_fraction")
    min_fraction = config.get_float("promotion.min_cv_folds_won", 0.75)
    if won_fraction is not None and won_fraction < min_fraction:
        return {
            "promote": False,
            "verdict": "REJECTED",
            "reason": (
                f"beat the baseline on the holdout but won only "
                f"{result['cv_folds_won']}/{result['cv_folds']} CV folds "
                f"({won_fraction:.0%}, threshold {min_fraction:.0%}). "
                f"CV MAE {result['cv_mae_mean']} vs baseline "
                f"{result['cv_baseline_mae_mean']}. One favourable window "
                f"is not evidence."
            ),
            "incumbent": incumbent,
        }

    # --- test 1c: is it stable enough to trust in production? ------------
    max_ratio = config.get_float("promotion.max_cv_std_ratio", 2.0)
    model_std = result.get("cv_mae_std")
    baseline_std = result.get("cv_baseline_mae_std")
    if model_std is not None and baseline_std:
        ratio = model_std / baseline_std
        if ratio > max_ratio:
            return {
                "promote": False,
                "verdict": "REJECTED",
                "reason": (
                    f"fold-to-fold spread {model_std} is {ratio:.1f}x the "
                    f"baseline's {baseline_std} (limit {max_ratio}x). A model "
                    f"that averages well but varies wildly fails "
                    f"unpredictably in production."
                ),
                "incumbent": incumbent,
            }

    # --- test 2: does it beat the incumbent by enough to be worth it? ---
    if incumbent is None:
        return {
            "promote": True,
            "verdict": "PROMOTED",
            "reason": "no incumbent champion; this model beats the baseline",
            "incumbent": None,
        }

    # A baseline incumbent IS persistence, and test 1 just measured
    # persistence on this run's test window. Comparing against the stored
    # figure instead would compare across datasets: when the data grew,
    # a model scoring 4.998 was rejected against a champion MAE of 4.726
    # measured on an older, easier window — even though it beat the
    # current baseline by 10%. Test 1 is the honest comparison here.
    if str(incumbent["model_id"]).startswith(BASELINE_PREFIX):
        return {
            "promote": True,
            "verdict": "PROMOTED",
            "reason": (f"beats the current persistence baseline "
                       f"({result['mae']} vs {baseline_mae}); the incumbent "
                       f"is that baseline, so no further comparison applies"),
            "incumbent": incumbent,
        }

    # For a trained incumbent, prefer a score measured on THIS run's test
    # window. `train_one` supplies it when the champion's model file and
    # feature set still load; the stored figure is the fallback and is
    # flagged as such.
    incumbent_mae = result.get("champion_mae_same_window")
    same_window = incumbent_mae is not None
    if not same_window:
        incumbent_mae = incumbent["mae"]

    if incumbent_mae is None or incumbent_mae <= 0:
        gain = 100.0
    else:
        gain = (incumbent_mae - result["mae"]) / incumbent_mae * 100

    basis = ("re-scored on this test window" if same_window
             else "STORED score, measured on other data")

    if gain < min_improvement:
        return {
            "promote": False,
            "verdict": "REJECTED",
            "reason": (
                f"improves on the champion by only {gain:+.2f}%, below the "
                f"{min_improvement}% threshold ({result['mae']} vs "
                f"{round(incumbent_mae, 4)}, {basis}). Churning the champion "
                f"on noise is worse than leaving it alone."
            ),
            "incumbent": incumbent,
            "gain_pct": round(gain, 2),
            "same_window": same_window,
        }

    return {
        "promote": True,
        "verdict": "PROMOTED",
        "reason": (f"beats the champion by {gain:+.2f}% "
                   f"({result['mae']} vs {round(incumbent_mae, 4)}, {basis})"),
        "incumbent": incumbent,
        "gain_pct": round(gain, 2),
        "same_window": same_window,
    }


def promote(model_id, target):
    """Make `model_id` the champion for `target`, demoting any incumbent."""
    execute_query(
        "UPDATE model_versions SET is_champion = 0 WHERE target = ?", (target,)
    )
    execute_query(
        "UPDATE model_versions SET is_champion = 1, promoted_at = ? "
        "WHERE model_id = ?",
        (datetime.now().isoformat(timespec="seconds"), model_id),
    )
    return model_id


def record_rejection(model_id, reason):
    execute_query(
        "UPDATE model_versions SET is_champion = 0, rejected_reason = ? "
        "WHERE model_id = ?",
        (reason, model_id),
    )


BASELINE_PREFIX = "baseline-persistence"


def promote_baseline(target, result):
    """Register persistence as the champion when no model earns the slot.

    Production needs a defined predictor. Leaving the registry empty
    because the ML model failed would mean the serving layer silently
    falls back to whatever was trained last — exactly what the gate exists
    to prevent. This makes the fallback explicit and measured.
    """
    model_id = f"{BASELINE_PREFIX}-{target}"
    execute_query(
        """
        INSERT OR REPLACE INTO model_versions
            (model_id, target, algorithm, params, feature_version,
             data_fingerprint, config_fingerprint, run_id, train_rows,
             test_rows, mae, rmse, baseline_mae, improvement_pct,
             inference_ms, is_champion, promoted_at, created_at)
        VALUES (?, ?, 'persistence', '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, 1, ?, ?)
        """,
        (model_id, target, result.get("feature_version"),
         result.get("data_fingerprint"), result.get("config_fingerprint"),
         result.get("run_id"), result.get("train_rows"), result.get("test_rows"),
         result.get("persistence_mae"), None, result.get("persistence_mae"),
         0.0, datetime.now().isoformat(timespec="seconds"),
         datetime.now().isoformat(timespec="seconds")),
    )
    execute_query(
        "UPDATE model_versions SET is_champion = 0 "
        "WHERE target = ? AND model_id != ?",
        (target, model_id),
    )
    execute_query(
        "UPDATE model_versions SET is_champion = 1, promoted_at = ? "
        "WHERE model_id = ?",
        (datetime.now().isoformat(timespec="seconds"), model_id),
    )
    return model_id


def run_gate(results, log_to_mlflow=True, allow_baseline_champion=True):
    """Log and gate every training result. Returns per-target decisions."""
    decisions = {}

    for target, result in results.items():
        if "error" in result:
            decisions[target] = {"promote": False, "verdict": "ERROR",
                                 "reason": result["error"]}
            continue

        if log_to_mlflow:
            try:
                result["mlflow_run_id"] = log_training_run(result)
            except Exception as exc:                       # noqa: BLE001
                result["mlflow_run_id"] = None
                result["mlflow_error"] = str(exc)

        decision = evaluate(result)
        decisions[target] = decision

        if decision["promote"]:
            promote(result["model_id"], target)
        else:
            record_rejection(result["model_id"], decision["reason"])

            if allow_baseline_champion:
                incumbent = current_champion(target)
                is_baseline_incumbent = (
                    incumbent is not None
                    and str(incumbent["model_id"]).startswith(BASELINE_PREFIX)
                )
                # Refresh an existing baseline champion as well as creating
                # a missing one. Persistence is re-measured on every run, so
                # leaving the old figure in place would have the drift
                # monitor comparing today's error against a reference taken
                # from different data — and reporting drift that is really
                # just a changed baseline.
                if incumbent is None or is_baseline_incumbent:
                    baseline_id = promote_baseline(target, result)
                    decision["baseline_champion"] = baseline_id
                    decision["baseline_refreshed"] = is_baseline_incumbent

    return decisions


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------
def registry(target=None):
    if target:
        rows = execute_query(
            """
            SELECT model_id, target, algorithm, mae, baseline_mae,
                   improvement_pct, is_champion, feature_version,
                   rejected_reason, created_at
            FROM model_versions WHERE target = ?
            ORDER BY created_at DESC
            """,
            (target,), fetch=True,
        )
    else:
        rows = execute_query(
            """
            SELECT model_id, target, algorithm, mae, baseline_mae,
                   improvement_pct, is_champion, feature_version,
                   rejected_reason, created_at
            FROM model_versions ORDER BY target, created_at DESC
            """, fetch=True,
        )
    return rows or []


def format_decisions(decisions):
    lines = ["=" * 84, "PROMOTION GATE", "=" * 84]
    for target, d in decisions.items():
        lines.append(f"\n  {target}: {d['verdict']}")
        lines.append(f"    {d['reason']}")
        if d.get("incumbent"):
            lines.append(f"    incumbent: {d['incumbent']['model_id']} "
                         f"(MAE {d['incumbent']['mae']})")
        if d.get("baseline_champion"):
            lines.append(f"    -> serving the BASELINE instead: "
                         f"{d['baseline_champion']}")
    lines.append("\n" + "=" * 84)
    return "\n".join(lines)


def format_registry(rows):
    lines = [
        "=" * 100,
        "MODEL REGISTRY",
        "=" * 100,
        f"  {'champ':6s} {'target':13s} {'algorithm':18s} {'MAE':>8} "
        f"{'baseline':>9} {'feat ver':13s} {'model_id'}",
    ]
    for r in rows:
        (model_id, target, algorithm, mae, baseline_mae,
         _improvement, is_champion, feature_version, rejected, _created) = r
        marker = "  *   " if is_champion else "      "
        lines.append(
            f"  {marker} {target:13s} {str(algorithm):18s} "
            f"{(f'{mae:.4f}' if mae is not None else '-'):>8} "
            f"{(f'{baseline_mae:.4f}' if baseline_mae is not None else '-'):>9} "
            f"{str(feature_version or '-'):13s} {model_id}"
        )
        if rejected:
            lines.append(f"          rejected: {rejected[:88]}")
    lines.append("=" * 100)
    lines.append("  * = champion (serves production traffic)")
    return "\n".join(lines)


if __name__ == "__main__":
    from model.features import load_clean_frame
    from model.forecast import train_all

    frame = load_clean_frame()
    if frame.empty:
        raise SystemExit("No cleaned data. Run: python -m pipeline.etl")

    print("Training challengers...\n")
    results = train_all(frame, verbose=False)

    decisions = run_gate(results)
    print(format_decisions(decisions))
    print()
    print(format_registry(registry()))
    print(f"\nMLflow UI: mlflow ui --backend-store-uri {config.MLFLOW_URI}")

"""
The full pipeline — every stage, in order, in one command.

    python -m orchestration.run_pipeline

    --source <spec>    read from somewhere other than the default
    --tune             search hyperparameters before training
    --skip-backtest    skip the walk-forward replay
    --quiet            summary only

Stage order matches the reference architecture:

     0  configuration        the settings every stage reads
     1  data sources         sources.py
     2  data engineering     schema.py
     4  data validation      validate.py          <- can stop the run
     5  data cleaning        clean.py
     6  data transformation  transform.py
     3  ETL load             etl.py
     7  feature engineering  features.py
     8  feature selection    selection.py
     9  feature scaling      scaling.py
    10  feature store        feature_store.py
    11  model training       tuning.py, forecast.py
    12  deployment           mlflow_tracker.py, predictor.py, drift.py
        evidence             backtest.py, recommender.py

Stages 1-6 run inside `pipeline.etl.run`, which owns their ordering and
writes the lineage record. This module sequences everything above them
and prints a stage-by-stage report.

A blocked quality gate stops the run. That is the point of a gate: a
pipeline that trains anyway on data it has just declared unfit is a
pipeline with a gate-shaped decoration.
"""

import argparse
import sys
import time
from datetime import datetime

import config


def banner(text):
    print(f"\n{'#' * 80}\n#  {text}\n{'#' * 80}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the full pipeline.")
    parser.add_argument("--source", default=None,
                        help="source spec, e.g. sqlite:// or csv://path")
    parser.add_argument("--tune", action="store_true",
                        help="search hyperparameters before training")
    parser.add_argument("--tune-budget", type=int, default=12)
    parser.add_argument("--skip-backtest", action="store_true")
    parser.add_argument("--skip-mlflow", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    verbose = not args.quiet
    started = time.perf_counter()
    summary = {}

    # ------------------------------------------------------------------
    banner("STAGE 0 — CONFIGURATION")
    print(f"  database          : {config.DB_PATH}")
    print(f"  config keys       : {len(config.all_values())} across "
          f"{len(set(k.split('.')[0] for k in config.all_values()))} categories")
    print(f"  config fingerprint: {config.fingerprint()}")
    print(f"  feature definition: {config.feature_fingerprint()}")

    # ------------------------------------------------------------------
    banner("STAGES 1-6 — DATA PLANE (extract, conform, validate, clean, transform, load)")
    from pipeline import etl

    etl_result = etl.run(args.source, verbose=True)
    summary["etl"] = etl_result

    if etl_result["status"] != "success":
        print(f"\n  PIPELINE STOPPED — ETL status '{etl_result['status']}'")
        if etl_result.get("blocked_reason"):
            print(f"  {etl_result['blocked_reason']}")
        return 1

    gate = etl_result["reports"]["gate"]
    if verbose:
        from pipeline.validate import format_report as gate_report

        print()
        print(gate_report(gate["checks"], gate["summary"]))

    run_id = etl_result["run_id"]
    fingerprint = etl_result["data_fingerprint"]

    from model.features import load_clean_frame

    frame = load_clean_frame(run_id)
    print(f"\n  cleaned frame: {len(frame)} rows, "
          f"{frame['segment_id'].nunique()} segment(s)")

    # ------------------------------------------------------------------
    banner("STAGE 7 — FEATURE ENGINEERING")
    from model.features import build_features, format_report as feature_report, targets

    metas = []
    for target in targets():
        _X, _y, meta = build_features(frame, target)
        metas.append(meta)
    print(feature_report(metas))
    summary["features"] = metas

    # ------------------------------------------------------------------
    banner("STAGE 8 — FEATURE SELECTION")
    from model.selection import format_report as selection_report, run_all as select_all

    selection = select_all(frame)
    print(selection_report(selection))
    print(f"\n  selection.enabled = {config.get_bool('selection.enabled')} "
          f"(training uses {'the selected subset' if config.get_bool('selection.enabled') else 'ALL features'})")
    summary["selection"] = selection

    # ------------------------------------------------------------------
    banner("STAGE 9 — FEATURE SCALING")
    from model.features import chronological_split
    from model.scaling import compare_methods, format_report as scaling_report, invariance_verdicts

    example = targets()[0]
    X, y, meta = build_features(frame, example)
    comparison = compare_methods(chronological_split(X, y, meta))
    verdicts = invariance_verdicts(comparison)
    print(f"  measured on {example}:")
    print(scaling_report(comparison, verdicts))
    summary["scaling"] = verdicts

    # ------------------------------------------------------------------
    banner("STAGE 10 — FEATURE STORE")
    from model.feature_store import (
        format_report as store_report, materialise_all, refresh_online,
    )

    manifests = materialise_all(frame, run_id=run_id, data_fingerprint=fingerprint)
    published = refresh_online(frame, data_fingerprint=fingerprint)
    print(store_report(manifests, published))
    summary["feature_store"] = manifests

    # ------------------------------------------------------------------
    if args.tune:
        banner("STAGE 11a — HYPERPARAMETER TUNING")
        from model.tuning import apply_best, format_report as tuning_report, run_all as tune_all

        tuned = tune_all(frame, n_iter=args.tune_budget)
        print(tuning_report(tuned))
        for target, result in tuned.items():
            outcome = apply_best(result)
            print(f"  {target:14s} {'ADOPTED' if outcome['applied'] else 'kept incumbent'}"
                  f" — {outcome.get('reason', outcome.get('params'))}")
        summary["tuning"] = tuned

    # ------------------------------------------------------------------
    banner("STAGE 11 — MODEL TRAINING AND EVALUATION")
    from model.forecast import train_all

    results = train_all(frame, run_id=run_id, data_fingerprint=fingerprint,
                        verbose=verbose)
    summary["training"] = results

    # ------------------------------------------------------------------
    banner("STAGE 12 — TRACKING, PROMOTION GATE, DEPLOYMENT")
    from tracking.mlflow_tracker import (
        format_decisions, format_registry, registry, run_gate,
    )

    decisions = run_gate(results, log_to_mlflow=not args.skip_mlflow)
    print(format_decisions(decisions))
    print()
    print(format_registry(registry()))
    summary["gate"] = decisions

    # ------------------------------------------------------------------
    banner("STAGE 12b — INFERENCE AND DRIFT")
    from serving.drift import format_report as drift_report, monitor
    from serving.predictor import backfill_all, format_predictions, predict_all, score

    predictions = predict_all(frame)
    print(format_predictions(predictions))

    backfilled = backfill_all(frame)
    scored = score(frame)
    print(f"\n  prediction log: "
          f"{sum(b.get('logged', 0) for b in backfilled.values())} logged, "
          f"{scored['scored']} scored against observed values")

    print()
    print(drift_report(monitor(frame)))
    summary["predictions"] = predictions

    # ------------------------------------------------------------------
    if not args.skip_backtest:
        banner("EVIDENCE — WALK-FORWARD BACKTEST")
        from evaluation.backtest import format_report as backtest_report, run_all as backtest_all

        backtest = backtest_all(frame)
        print(backtest_report(backtest))
        summary["backtest"] = backtest

    # ------------------------------------------------------------------
    banner("RECOMMENDATION AND COST")
    from service.recommender import print_report

    summary["recommendation"] = print_report(frame)

    # ------------------------------------------------------------------
    elapsed = time.perf_counter() - started
    banner("PIPELINE COMPLETE")
    print(f"  finished at   : {datetime.now().isoformat(timespec='seconds')}")
    print(f"  elapsed       : {elapsed:.1f}s")
    print(f"  etl run_id    : {run_id}")
    print(f"  data          : {fingerprint}")
    print(f"  config        : {config.fingerprint()}")

    print("\n  champions serving production:")
    from tracking.lineage import champions

    for _, row in champions().iterrows():
        print(f"    {row['target']:14s} {row['algorithm']:18s} "
              f"MAE {row['mae']}   {row['model_id']}")

    print("\n  next: streamlit run dashboard/app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

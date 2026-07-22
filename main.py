"""
Menu-driven entry point.

Every option here maps to a module that also runs standalone with
`python -m <module>`; this exists so the whole system can be driven
without remembering twelve commands.

    python main.py
"""

import config


def _pause():
    input("\n  [enter] to continue ")


def _frame():
    """The latest cleaned frame, or None with an explanation."""
    from model.features import load_clean_frame

    frame = load_clean_frame()
    if frame.empty:
        print("\n  No cleaned data yet. Run option 2 (ETL) first.")
        return None
    return frame


# ----------------------------------------------------------------------
def collector_menu():
    from collector.psutil_logger import log_once, run_logger
    from crud.metrics_crud import count_metrics

    while True:
        print("\n---------- Collector ----------")
        print(f"  rows in metrics: {count_metrics()}")
        print("1. Log one sample now")
        print("2. Run logger for a fixed duration")
        print("3. Run logger continuously (Ctrl+C to stop)")
        print("4. Run the load generator (produces a forecastable signal)")
        print("5. Back")

        match input("Enter your choice: ").strip():
            case "1":
                log_once()
            case "2":
                run_logger(duration=int(input("Duration in seconds: ")))
            case "3":
                run_logger()
            case "4":
                from collector.load_generator import main as generate

                generate()
            case "5":
                return
            case _:
                print("Invalid choice.")


def data_menu():
    from crud.metrics_crud import (
        count_metrics, delete_metric, purge_before, read_all, read_latest,
        update_metric,
    )

    def show(rows):
        if not rows:
            print("No records found.")
            return
        print(f"\n{'ID':<5}{'Timestamp':<22}{'CPU%':>7}{'MEM%':>7}{'DISK%':>7}")
        print("-" * 50)
        for r in rows:
            print(f"{r[0]:<5}{r[1]:<22}{r[2]:>7.1f}{r[3]:>7.1f}{r[5]:>7.1f}")

    while True:
        print("\n---------- Data / CRUD ----------")
        print("1. View all records")
        print("2. View latest N records")
        print("3. Total record count")
        print("4. Update a field on a record")
        print("5. Delete a record")
        print("6. Purge records before a timestamp")
        print("7. Export to CSV")
        print("8. Back")

        match input("Enter your choice: ").strip():
            case "1":
                show(read_all())
            case "2":
                show(read_latest(int(input("How many? "))))
            case "3":
                print("Total records:", count_metrics())
            case "4":
                record_id = int(input("Record ID: "))
                field = input("Field: ").strip()
                print("Updated." if update_metric(field=field, metric_id=record_id,
                                                  value=float(input("New value: ")))
                      else "Update failed.")
            case "5":
                print("Deleted." if delete_metric(int(input("Record ID: ")))
                      else "Delete failed.")
            case "6":
                cutoff = input("Delete records before (ISO timestamp): ").strip()
                print(f"Purged {purge_before(cutoff)} record(s).")
            case "7":
                from conversion.csv_export import export_all

                for name, result in export_all().items():
                    print(f"  {name:22s} {result.get('path', result.get('error'))}")
            case "8":
                return
            case _:
                print("Invalid choice.")


def pipeline_menu():
    while True:
        print("\n---------- Pipeline (stages 1-6) ----------")
        print("1. Show available data sources")
        print("2. Validate current data (quality gate)")
        print("3. Run the ETL (extract -> clean -> transform -> load)")
        print("4. Show ETL run history")
        print("5. Back")

        match input("Enter your choice: ").strip():
            case "1":
                from pipeline.sources import describe_available

                for entry in describe_available():
                    print(f"  {entry['scheme']:12s} {entry['status']:34s} "
                          f"{entry['detail']}")
            case "2":
                from pipeline.schema import coerce
                from pipeline.sources import get_source
                from pipeline.validate import format_report, run_gate

                checks, summary = run_gate(coerce(get_source().read()))
                print(format_report(checks, summary))
            case "3":
                from pipeline.etl import format_report, run

                print(format_report(run(verbose=True)))
            case "4":
                from pipeline.etl import run_history

                for r in run_history(15):
                    print(f"  run {r[0]:>3}  {str(r[1]):20s} {str(r[3]):8s} "
                          f"{str(r[4]):>5} -> {str(r[5]):>5}  "
                          f"gate={str(r[6]):5s} {r[7]}")
            case "5":
                return
            case _:
                print("Invalid choice.")


def model_menu():
    while True:
        print("\n---------- Model (stages 7-11) ----------")
        print("1. Feature engineering report")
        print("2. Feature selection")
        print("3. Feature scaling comparison")
        print("4. Feature store (materialise + publish online)")
        print("5. Baseline ladder")
        print("6. Train all models")
        print("7. Tune hyperparameters")
        print("8. Back")

        choice = input("Enter your choice: ").strip()
        if choice == "8":
            return

        frame = _frame()
        if frame is None:
            continue

        match choice:
            case "1":
                from model.features import build_features, format_report, targets

                print(format_report([build_features(frame, t)[2]
                                     for t in targets()]))
            case "2":
                from model.selection import format_report, run_all

                print(format_report(run_all(frame)))
            case "3":
                from model.features import build_features, chronological_split, targets
                from model.scaling import (
                    compare_methods, format_report, invariance_verdicts,
                )

                X, y, meta = build_features(frame, targets()[0])
                table = compare_methods(chronological_split(X, y, meta))
                print(format_report(table, invariance_verdicts(table)))
            case "4":
                from model.feature_store import (
                    format_report, materialise_all, refresh_online,
                )
                from pipeline.etl import latest_run

                run = latest_run() or {}
                print(format_report(
                    materialise_all(frame, run.get("run_id"),
                                    run.get("data_fingerprint")),
                    refresh_online(frame, data_fingerprint=run.get("data_fingerprint")),
                ))
            case "5":
                import numpy as np

                from model.baseline import run_ladder
                from model.features import build_features, chronological_split, targets

                for target in targets():
                    X, y, meta = build_features(frame, target)
                    if X.empty:
                        continue
                    split = chronological_split(X, y, meta)
                    index = np.arange(len(X))[split["split_index"]:]
                    table, _ = run_ladder(split, target, meta["actual_now"], index)
                    print(f"\n=== {target} ===")
                    print(table.to_string(index=False))
            case "6":
                from model.forecast import train_all

                train_all(frame, verbose=True)
            case "7":
                from model.tuning import format_report, run_all

                budget = int(input("Search budget per target [12]: ") or 12)
                print(format_report(run_all(frame, n_iter=budget)))
            case _:
                print("Invalid choice.")
        _pause()


def serving_menu():
    while True:
        print("\n---------- Serving (stage 12) ----------")
        print("1. Run the promotion gate (train + gate + register)")
        print("2. Show the model registry")
        print("3. Predict now (champions)")
        print("4. Multi-step horizon forecast")
        print("5. Drift monitor")
        print("6. Trace lineage of a champion")
        print("7. Back")

        choice = input("Enter your choice: ").strip()
        if choice == "7":
            return

        frame = _frame()
        if frame is None:
            continue

        match choice:
            case "1":
                from model.forecast import train_all
                from tracking.mlflow_tracker import format_decisions, run_gate

                print(format_decisions(run_gate(train_all(frame, verbose=False))))
            case "2":
                from tracking.mlflow_tracker import format_registry, registry

                print(format_registry(registry()))
            case "3":
                from serving.predictor import format_predictions, predict_all

                print(format_predictions(predict_all(frame)))
            case "4":
                from model.horizon import horizon_summary
                from model.features import targets

                for target in targets():
                    summary = horizon_summary(target, df=frame)
                    if summary:
                        print(f"  {target:14s} peak={summary['forecast_peak']}%  "
                              f"P95={summary['forecast_p95']}%  "
                              f"[{summary['predictor']}]")
            case "5":
                from serving.drift import format_report, monitor

                print(format_report(monitor(frame)))
            case "6":
                from tracking.lineage import champions, format_trace, trace_model

                for model_id in champions()["model_id"]:
                    print(format_trace(trace_model(model_id)))
            case _:
                print("Invalid choice.")
        _pause()


def evidence_menu():
    while True:
        print("\n---------- Recommendation & evidence ----------")
        print("1. Allocation recommendation + cost")
        print("2. Walk-forward backtest (measured savings)")
        print("3. Cost model breakdown")
        print("4. Cost vs SLA tradeoff")
        print("5. Back")

        choice = input("Enter your choice: ").strip()
        if choice == "5":
            return

        frame = _frame()
        if frame is None:
            continue

        match choice:
            case "1":
                from service.recommender import print_report

                print_report(frame)
            case "2":
                from evaluation.backtest import format_report, run_all

                print(format_report(run_all(frame)))
            case "3":
                from service.cost_model import static_cost

                for key, value in static_cost().items():
                    print(f"  {key:9s} ${value}")
            case "4":
                from model.features import targets
                from service.recommender import tradeoff_curve

                for target in targets():
                    _curve, minimum = tradeoff_curve(target, frame)
                    if minimum:
                        print(f"  {target:14s} cheapest compliant "
                              f"{minimum['allocation_percent']}% -> "
                              f"${minimum['monthly_cost']}/mo "
                              f"({minimum['breach_rate']}% breaches)")
            case _:
                print("Invalid choice.")
        _pause()


def config_menu():
    from crud import config_crud

    while True:
        print("\n---------- Configuration ----------")
        print(f"  {len(config.all_values())} settings, fingerprint "
              f"{config.fingerprint()}")
        print("1. Browse a category")
        print("2. Change a setting")
        print("3. Change history")
        print("4. Show the schema contract")
        print("5. Back")

        match input("Enter your choice: ").strip():
            case "1":
                categories = config_crud.categories()
                for index, name in enumerate(categories, 1):
                    print(f"  {index}. {name}")
                pick = int(input("Category number: ")) - 1
                for row in config_crud.describe(categories[pick]):
                    print(f"  {row[0]:38s} {str(row[1])[:24]:26s} {row[4] or ''}"[:110])
            case "2":
                key = input("Setting key: ").strip()
                try:
                    print(f"  current: {config.get(key)}")
                    config.set_value(key, input("New value: ").strip(),
                                     source="main.py")
                    print(f"  now: {config.get(key)}   "
                          f"fingerprint {config.fingerprint()}")
                except (KeyError, ValueError) as exc:
                    print(f"  {exc}")
            case "3":
                for row in config_crud.history(limit=20):
                    print(f"  {row[3]}  {row[0]:34s} {row[1]} -> {row[2]}  "
                          f"({row[4]})")
            case "4":
                from pipeline.schema import contract

                for rule in contract():
                    print(f"  {rule['column']:14s} {rule['dtype']:9s} "
                          f"null={rule['nullable']!s:5s} "
                          f"range=[{rule['min_value']}, {rule['max_value']}] "
                          f"target={rule['is_target']}")
            case "5":
                return
            case _:
                print("Invalid choice.")


# ----------------------------------------------------------------------
def main():
    while True:
        print("\n" + "=" * 56)
        print("  PREDICTIVE RESOURCE MONITORING SYSTEM")
        print("=" * 56)
        print("1. Collector          (gather metrics)")
        print("2. Data / CRUD        (view, edit, export)")
        print("3. Pipeline           (stages 1-6: ETL + quality gate)")
        print("4. Model              (stages 7-11: features -> training)")
        print("5. Serving            (stage 12: gate, predict, drift)")
        print("6. Recommendation     (allocation, cost, backtest)")
        print("7. Configuration      (every setting, from the database)")
        print("8. RUN THE FULL PIPELINE")
        print("9. Exit")

        match input("Enter your choice: ").strip():
            case "1":
                collector_menu()
            case "2":
                data_menu()
            case "3":
                pipeline_menu()
            case "4":
                model_menu()
            case "5":
                serving_menu()
            case "6":
                evidence_menu()
            case "7":
                config_menu()
            case "8":
                from orchestration.run_pipeline import main as run_pipeline

                run_pipeline([])
                _pause()
            case "9":
                print("Exiting...")
                return
            case _:
                print("Invalid choice.")


if __name__ == "__main__":
    main()

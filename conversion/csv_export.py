"""
CSV export — the data plane in reverse.

Stage 1 defines a `CSVSource` that reads metrics from a file. This writes
one, which makes the round trip real: export a run, point the pipeline at
the file, and everything replays from it with no other change.

    python -m conversion.csv_export
    python -m pipeline.etl "csv://data/exports/metrics_raw.csv"

Four things can be exported: the raw table, a cleaned ETL run, a
retention tier, and a materialised feature-store version.
"""

import argparse
import os

import config
from crud.query import execute_query

EXPORT_DIR = os.path.join(config.DATA_DIR, "exports")


def _write(df, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)
    return {"path": path, "rows": len(df), "columns": len(df.columns)}


def export_raw(path=None):
    from pipeline.sources import SQLiteSource

    path = path or os.path.join(EXPORT_DIR, "metrics_raw.csv")
    return _write(SQLiteSource().read(), path)


def export_clean(run_id=None, path=None):
    from pipeline.etl import latest_run, read_clean

    if run_id is None:
        latest = latest_run()
        if latest is None:
            return {"error": "no successful ETL run to export"}
        run_id = latest["run_id"]

    path = path or os.path.join(EXPORT_DIR, f"metrics_clean_run{run_id}.csv")
    return _write(read_clean(run_id), path)


def export_rollup(tier="1min", run_id=None, path=None):
    import pandas as pd

    from pipeline.etl import latest_run

    if run_id is None:
        latest = latest_run()
        if latest is None:
            return {"error": "no successful ETL run to export"}
        run_id = latest["run_id"]

    rows = execute_query(
        """
        SELECT ts, column_name, mean_value, max_value, p95_value, samples
        FROM metrics_rollup WHERE run_id = ? AND tier = ?
        ORDER BY ts, column_name
        """,
        (run_id, tier), fetch=True,
    ) or []
    if not rows:
        return {"error": f"no '{tier}' rollup for run {run_id}"}

    frame = pd.DataFrame(rows, columns=[
        "ts", "column_name", "mean", "max", "p95", "samples",
    ])
    path = path or os.path.join(EXPORT_DIR, f"rollup_{tier}_run{run_id}.csv")
    return _write(frame, path)


def export_features(target=None, version=None, path=None):
    from model.feature_store import load_offline

    target = target or config.get_json("features.targets")[0]
    X, y, manifest = load_offline(version=version, target=target)
    if X.empty:
        return {"error": f"no materialised features for {target}"}

    frame = X.copy()
    frame.insert(0, "ts", manifest["timestamps"])
    frame["__target__"] = y.values

    path = path or os.path.join(
        EXPORT_DIR, f"features_{target}_{manifest['version_id']}.csv"
    )
    return _write(frame, path)


def export_all():
    return {
        "raw": export_raw(),
        "clean": export_clean(),
        "rollup_1min": export_rollup("1min"),
        **{f"features_{t}": export_features(t)
           for t in config.get_json("features.targets")},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export data to CSV.")
    parser.add_argument("what", nargs="?", default="all",
                        choices=["all", "raw", "clean", "rollup", "features"])
    parser.add_argument("--tier", default="1min")
    parser.add_argument("--target", default=None)
    parser.add_argument("--run-id", type=int, default=None)
    args = parser.parse_args()

    if args.what == "all":
        results = export_all()
    elif args.what == "raw":
        results = {"raw": export_raw()}
    elif args.what == "clean":
        results = {"clean": export_clean(args.run_id)}
    elif args.what == "rollup":
        results = {"rollup": export_rollup(args.tier, args.run_id)}
    else:
        results = {"features": export_features(args.target)}

    print("CSV EXPORT")
    for name, result in results.items():
        if "error" in result:
            print(f"  {name:22s} {result['error']}")
        else:
            print(f"  {name:22s} {result['rows']:>5} rows x "
                  f"{result['columns']:>2} cols -> {result['path']}")

    print('\n  Replay an export with: python -m pipeline.etl "csv://<path>"')

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
RAW_PATH = os.path.join(EXPORT_DIR, "metrics_raw.csv")


def _write(df, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)
    return {"path": path, "rows": len(df), "columns": len(df.columns)}


def export_raw(path=None):
    from pipeline.sources import SQLiteSource

    path = path or RAW_PATH
    return _write(SQLiteSource().read(), path)


# ----------------------------------------------------------------------
# Mirroring — the raw CSV as a continuous consequence of writing, not a
# thing somebody has to remember to do.
#
# `crud.metrics_crud` calls these on every write, so metrics_raw.csv is
# always the table rather than a snapshot of whenever the export menu was
# last used. An insert appends one line, because the collector inserts
# every few seconds and rewriting thousands of rows on that path would
# make sampling cost grow with history. Edits and deletes cannot be
# expressed as an append, so those rewrite the file — they are manual,
# occasional operations where an O(n) rewrite costs nothing.
#
# Nothing here may raise. A read-only export directory or a file held open
# by Excel is a reason for the CSV to fall behind, never a reason for the
# database write to fail or the collector to stop.
# ----------------------------------------------------------------------
def _mirror_enabled():
    try:
        return config.get_bool("export.mirror_raw_csv", True)
    except Exception:                                         # noqa: BLE001
        return False


# Set when a mirror write fails, cleared when one succeeds. Appending is
# blind — it trusts that every earlier row reached the file — so a single
# failed write would otherwise leave the CSV one row short forever, with
# every later append landing happily after the hole. Remembering the miss
# turns the next insert into a full rebuild, which is the only operation
# that can close a gap it cannot see. Costs nothing when nothing fails.
_stale = False


def mirror_insert(record, row_id=None):
    """Append one just-inserted metrics row to the raw CSV."""
    global _stale

    if not _mirror_enabled():
        # Rows written while mirroring is off are rows the CSV never saw.
        _stale = True
        return None
    try:
        # No file yet, an empty one, or a known-missed write: a full export
        # is what creates the header, and appending to a headerless file
        # would misalign every column for good.
        if (_stale
                or not os.path.exists(RAW_PATH)
                or os.path.getsize(RAW_PATH) == 0):
            result = export_raw()
            _stale = False
            return result

        import pandas as pd

        with open(RAW_PATH, "r", encoding="utf-8") as handle:
            header = handle.readline().strip().split(",")
        if not header or header == [""]:
            return export_raw()

        values = dict(record)
        if row_id is not None:
            values["id"] = row_id

        # Written through pandas, and ordered by the header already on
        # disk, so the appended line matches the file's own column order
        # and line terminator rather than assuming either.
        row = pd.DataFrame([{c: values.get(c) for c in header}], columns=header)
        row.to_csv(RAW_PATH, mode="a", header=False, index=False)
        _stale = False
        return {"path": RAW_PATH, "appended": 1}
    except Exception as exc:                                  # noqa: BLE001
        _stale = True
        print(f"  (raw CSV mirror skipped, will rebuild next write: {exc})")
        return None


def mirror_rebuild():
    """Rewrite the raw CSV after an update, delete or purge."""
    global _stale

    if not _mirror_enabled():
        _stale = True
        return None
    try:
        result = export_raw()
        _stale = False
        return result
    except Exception as exc:                                  # noqa: BLE001
        _stale = True
        print(f"  (raw CSV rebuild skipped, will rebuild next write: {exc})")
        return None


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

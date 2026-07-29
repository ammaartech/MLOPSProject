"""
STAGE 3 — ETL Pipeline: extract, transform, load automation.

The only module that knows what order the data-plane stages go in:

    extract    sources.get_source(spec).read()          [1]
    conform    schema.coerce()                           [2]
    validate   validate.run_gate()                       [4]  <- may stop here
    clean      clean.clean()                             [5]
    transform  transform.transform()                     [6]
    load       metrics_clean + metrics_rollup

Every execution opens a row in `pipeline_runs` and closes it with a
status, whether it succeeded, was blocked by the quality gate, or raised.
A run that fails still leaves a record saying why — a pipeline that goes
quiet when something breaks is worse than one that fails loudly.

Lineage recorded per run
------------------------
    data_fingerprint    hash of the input rows
    config_fingerprint  hash of the configuration in force
    gate_verdict        PASS / WARN / FAIL and the reason
    rows_in / rows_out  what came in and what survived

A model trained later records the run_id it consumed, so any prediction
can be traced back to the exact input data and settings that produced it.
"""

import hashlib
import traceback
from datetime import datetime

import pandas as pd

import config
from crud.query import execute_insert, execute_many, execute_query
from pipeline import clean as clean_mod
from pipeline import schema as schema_mod
from pipeline import sources as sources_mod
from pipeline import transform as transform_mod
from pipeline import validate as validate_mod


# ----------------------------------------------------------------------
# Fingerprinting
# ----------------------------------------------------------------------
def data_fingerprint(df):
    """Short stable hash of the input data.

    Hashes CONTENT — row count, span, and the target values — rather than
    anything about how the data was stored. Two runs over the same rows
    produce the same fingerprint regardless of source connector.
    """
    if df is None or df.empty:
        return "empty"

    targets = [c for c in schema_mod.target_columns() if c in df.columns]
    payload = "|".join([
        str(len(df)),
        str(df["ts"].min()),
        str(df["ts"].max()),
        *[f"{c}:{pd.to_numeric(df[c], errors='coerce').sum():.4f}" for c in targets],
    ])
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


# ----------------------------------------------------------------------
# Run lifecycle
# ----------------------------------------------------------------------
def start_run(source_kind, source_detail):
    return execute_insert(
        """
        INSERT INTO pipeline_runs
            (started_at, source_kind, source_detail, status, config_fingerprint)
        VALUES (?, ?, ?, 'running', ?)
        """,
        (datetime.now().isoformat(timespec="seconds"), source_kind,
         source_detail, config.fingerprint()),
    )


def finish_run(run_id, status, rows_in=None, rows_out=None, gate_verdict=None,
               gate_detail=None, data_fp=None, error=None):
    execute_query(
        """
        UPDATE pipeline_runs
           SET finished_at = ?, status = ?, rows_in = ?, rows_out = ?,
               gate_verdict = ?, gate_detail = ?, data_fingerprint = ?, error = ?
         WHERE run_id = ?
        """,
        (datetime.now().isoformat(timespec="seconds"), status, rows_in, rows_out,
         gate_verdict, gate_detail, data_fp, error, run_id),
    )


def latest_run(status="success"):
    rows = execute_query(
        "SELECT run_id, started_at, rows_out, gate_verdict, data_fingerprint "
        "FROM pipeline_runs WHERE status = ? ORDER BY run_id DESC LIMIT 1",
        (status,), fetch=True,
    )
    if not rows:
        return None
    r = rows[0]
    return {"run_id": r[0], "started_at": r[1], "rows_out": r[2],
            "gate_verdict": r[3], "data_fingerprint": r[4]}


def run_history(limit=20):
    return execute_query(
        """
        SELECT run_id, started_at, finished_at, source_kind, rows_in, rows_out,
               gate_verdict, status, data_fingerprint, config_fingerprint
        FROM pipeline_runs ORDER BY run_id DESC LIMIT ?
        """,
        (int(limit),), fetch=True,
    ) or []


# ----------------------------------------------------------------------
# Load
# ----------------------------------------------------------------------
def load_clean(run_id, df):
    """Write the cleaned series to `metrics_clean` for this run."""
    columns = ["cpu_percent", "mem_percent", "mem_used_mb",
               "disk_read_mb_s", "disk_write_mb_s"]
    rows = [
        (
            run_id,
            row["ts"].isoformat() if hasattr(row["ts"], "isoformat") else str(row["ts"]),
            int(row["segment_id"]),
            *[_nullable_float(row.get(c)) for c in columns],
            row.get("regime"),
            int(row.get("is_imputed", 0) or 0),
            int(row.get("is_outlier", 0) or 0),
        )
        for _, row in df.iterrows()
    ]

    connection = _bulk_insert(
        """
        INSERT OR REPLACE INTO metrics_clean
            (run_id, ts, segment_id, cpu_percent, mem_percent, mem_used_mb,
             disk_read_mb_s, disk_write_mb_s, regime, is_imputed, is_outlier)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return connection


def load_rollups(run_id, tiers):
    """Write every retention tier in long format."""
    rows = []
    for tier, frame in tiers.items():
        if frame is None or frame.empty:
            continue
        base_columns = sorted({
            c.rsplit("_", 1)[0] for c in frame.columns
            if c.endswith(("_mean", "_max", "_p95"))
        })
        for _, row in frame.iterrows():
            ts = row["ts"]
            ts = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            for col in base_columns:
                rows.append((
                    run_id, tier, ts, col,
                    _nullable_float(row.get(f"{col}_mean")),
                    _nullable_float(row.get(f"{col}_max")),
                    _nullable_float(row.get(f"{col}_p95")),
                    int(row.get("samples", 0) or 0),
                ))

    return _bulk_insert(
        """
        INSERT OR REPLACE INTO metrics_rollup
            (run_id, tier, ts, column_name, mean_value, max_value,
             p95_value, samples)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def prune_orphaned_loads():
    """Delete loaded rows whose run never completed successfully.

    A crashed or blocked run can leave partial data behind. Without this,
    `metrics_clean` accumulates rows belonging to runs that are not in the
    lineage table, and any query that forgets to filter by run_id silently
    trains on a mixture of them.
    """
    removed = {}
    for table in ("metrics_clean", "metrics_rollup"):
        removed[table] = execute_query(
            f"""
            DELETE FROM {table}
             WHERE run_id NOT IN (
                   SELECT run_id FROM pipeline_runs WHERE status = 'success'
             )
            """
        ) or 0
    return removed


def read_clean(run_id=None):
    """Read a cleaned run back as a DataFrame. Defaults to the latest."""
    if run_id is None:
        latest = latest_run()
        if latest is None:
            return pd.DataFrame()
        run_id = latest["run_id"]

    rows = execute_query(
        """
        SELECT ts, segment_id, cpu_percent, mem_percent, mem_used_mb,
               disk_read_mb_s, disk_write_mb_s, regime, is_imputed, is_outlier
        FROM metrics_clean WHERE run_id = ? ORDER BY ts ASC
        """,
        (run_id,), fetch=True,
    ) or []

    df = pd.DataFrame(rows, columns=[
        "ts", "segment_id", "cpu_percent", "mem_percent", "mem_used_mb",
        "disk_read_mb_s", "disk_write_mb_s", "regime", "is_imputed", "is_outlier",
    ])
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"])
    return df


# ----------------------------------------------------------------------
# The pipeline
# ----------------------------------------------------------------------
def run(source_spec=None, persist=True, verbose=True):
    """Execute stages 1-6 and load the result. Returns a result dict."""
    prune_orphaned_loads()

    source = sources_mod.get_source(source_spec)
    description = source.describe()
    run_id = start_run(description["kind"], description["detail"])

    result = {
        "run_id": run_id,
        "source": description,
        "status": "running",
        "reports": {},
    }

    try:
        # --- [1] extract ------------------------------------------------
        raw = source.read()
        result["rows_in"] = len(raw)
        if verbose:
            print(f"[1] extract    : {len(raw)} rows from {description['kind']}")

        if raw.empty:
            finish_run(run_id, "empty", rows_in=0, rows_out=0,
                       gate_verdict="FAIL", gate_detail="source returned no rows")
            result.update(status="empty", rows_out=0)
            return result

        # --- [2] conform to the contract --------------------------------
        raw = schema_mod.coerce(raw)
        schema_report = schema_mod.validate(raw)
        result["reports"]["schema"] = schema_report
        fingerprint = data_fingerprint(raw)
        result["data_fingerprint"] = fingerprint
        if verbose:
            print(f"[2] conform    : {'CONFORMS' if schema_report['conforms'] else 'BREACHES'}"
                  f"  fingerprint={fingerprint}")

        # --- [4] validate ------------------------------------------------
        checks, gate = validate_mod.run_gate(raw)
        result["reports"]["gate"] = {"checks": checks, "summary": gate}
        if persist:
            validate_mod.persist(run_id, checks)
        if verbose:
            print(f"[4] validate   : {gate['verdict']} "
                  f"({gate['passed']} pass / {gate['warned']} warn / "
                  f"{gate['failed']} fail)")

        if not gate["should_proceed"]:
            reasons = "; ".join(
                c["detail"] for c in checks if c["status"] == validate_mod.FAIL
            )
            finish_run(run_id, "blocked", rows_in=len(raw), rows_out=0,
                       gate_verdict=gate["verdict"], gate_detail=reasons,
                       data_fp=fingerprint)
            result.update(status="blocked", rows_out=0, blocked_reason=reasons)
            if verbose:
                print(f"    BLOCKED    : {reasons}")
            return result

        # --- [5] clean ---------------------------------------------------
        cleaned, clean_report = clean_mod.clean(raw, gate["cadence_seconds"])
        result["reports"]["clean"] = clean_report
        if verbose:
            print(f"[5] clean      : {clean_report['rows_in']} -> "
                  f"{clean_report['rows_out']} rows, "
                  f"{clean_report['segments']} segment(s), "
                  f"{clean_report['synthetic_rows_added']} synthetic")

        # --- [6] transform -----------------------------------------------
        transformed, tiers, transform_report = transform_mod.transform(cleaned)
        result["reports"]["transform"] = transform_report
        if verbose:
            print(f"[6] transform  : {transform_report['columns_out']} columns, "
                  f"tiers {transform_report['rollup_tiers']}")

        # --- load ---------------------------------------------------------
        if persist:
            load_clean(run_id, transformed)
            load_rollups(run_id, tiers)
            if verbose:
                print(f"[3] load       : metrics_clean + metrics_rollup "
                      f"(run_id={run_id})")

        finish_run(run_id, "success", rows_in=len(raw), rows_out=len(transformed),
                   gate_verdict=gate["verdict"],
                   gate_detail=f"{gate['passed']}P/{gate['warned']}W/{gate['failed']}F",
                   data_fp=fingerprint)

        result.update(status="success", rows_out=len(transformed),
                      frame=transformed, rollups=tiers)
        return result

    except Exception as exc:                      # noqa: BLE001
        finish_run(run_id, "error", error=f"{type(exc).__name__}: {exc}")
        result.update(status="error", error=str(exc),
                      traceback=traceback.format_exc())
        if verbose:
            print(f"    ERROR      : {type(exc).__name__}: {exc}")
        raise


# ----------------------------------------------------------------------
def _nullable_float(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return None if pd.isna(value) else float(value)
    except (TypeError, ValueError):
        return None


def _bulk_insert(sql, rows):
    """One connection for many rows. See crud.query.execute_many."""
    return execute_many(sql, rows)


def format_report(result):
    lines = [
        "",
        "#" * 78,
        f"#  ETL RUN {result['run_id']} — {result['status'].upper()}",
        "#" * 78,
        f"  source           : {result['source']['kind']} "
        f"({result['source']['detail']})",
        f"  rows in / out    : {result.get('rows_in', 0)} -> {result.get('rows_out', 0)}",
        f"  data fingerprint : {result.get('data_fingerprint', '-')}",
        f"  config fingerprint: {config.fingerprint()}",
    ]
    if result["status"] == "blocked":
        lines.append(f"  blocked because  : {result['blocked_reason']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    spec = sys.argv[1] if len(sys.argv) > 1 else None
    outcome = run(spec)
    print(format_report(outcome))

    print("\nRun history:")
    print(f"  {'run':>4} {'started':20s} {'src':8s} {'in':>6} {'out':>6} "
          f"{'gate':6s} {'status':9s} {'data fp':13s}")
    for r in run_history(10):
        print(f"  {r[0]:>4} {str(r[1]):20s} {str(r[3]):8s} "
              f"{str(r[4] or '-'):>6} {str(r[5] or '-'):>6} "
              f"{str(r[6] or '-'):6s} {str(r[7]):9s} {str(r[8] or '-'):13s}")

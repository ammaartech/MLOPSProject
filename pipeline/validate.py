"""
STAGE 4 — Data Validation.

Null checks, duplicate checks and business rules, run before anything
trains on the data. Every threshold is read from the `config` table and
every structural rule from `schema_contract`, so tightening the gate is a
row edit rather than a code change.

Each check returns one of three verdicts:

    PASS   the data satisfies the rule
    WARN   worth knowing, does not by itself stop the pipeline
    FAIL   the data is not fit to model; the run should stop

The distinction matters. A constant disk column is a WARN — it is real,
it is just not forecastable, and saying so is a finding rather than an
error. Fifty rows of data is a FAIL, because nothing downstream can
produce a defensible number from it.

Results are written to `quality_checks` against the run id, so data health
is queryable over time rather than only visible in a console.
"""

from datetime import datetime, timedelta

import pandas as pd

import config
from crud.query import execute_query
from pipeline import schema as schema_mod

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _check(name, status, detail, value=None):
    return {"check": name, "status": status, "detail": detail, "value": value}


# ----------------------------------------------------------------------
# Cadence and gaps — used by the gate and by stage 5
# ----------------------------------------------------------------------
def detect_cadence(df):
    """The real sampling interval in seconds, from the data itself.

    The configured cadence is a statement of intent; this is what the
    collector actually did. Everything gap-related keys off the measured
    value, because a mismatch between the two is exactly the condition
    that silently corrupts lag features.
    """
    if df is None or len(df) < 3 or "ts" not in df.columns:
        return float(config.get_int("pipeline.nominal_cadence_sec"))

    ts = pd.to_datetime(df["ts"], errors="coerce").dropna().sort_values()
    if len(ts) < 3:
        return float(config.get_int("pipeline.nominal_cadence_sec"))

    deltas = ts.diff().dt.total_seconds().dropna()
    deltas = deltas[deltas > 0]
    if deltas.empty:
        return float(config.get_int("pipeline.nominal_cadence_sec"))
    return float(deltas.median())


def find_gaps(df, cadence=None):
    """Breaks in collection: intervals longer than gap_multiple x cadence.

    Returns one row per gap. These are not slow samples — they are periods
    where the collector was not running, and any feature window spanning
    one would claim a value from before the break is recent history.
    """
    if df is None or df.empty or "ts" not in df.columns:
        return pd.DataFrame(columns=["gap_start", "gap_end", "gap_seconds",
                                     "missed_samples"])

    cadence = cadence or detect_cadence(df)
    threshold = cadence * config.get_float("pipeline.gap_multiple")

    ts = pd.to_datetime(df["ts"], errors="coerce").dropna().sort_values()
    deltas = ts.diff().dt.total_seconds()

    gaps = []
    for position in range(1, len(ts)):
        delta = deltas.iloc[position]
        if pd.notna(delta) and delta > threshold:
            gaps.append({
                "gap_start": ts.iloc[position - 1],
                "gap_end": ts.iloc[position],
                "gap_seconds": round(float(delta), 1),
                "missed_samples": int(delta // cadence) - 1,
            })
    return pd.DataFrame(gaps)


def profile(df):
    """Per-column summary: nulls, range, variability, distinct values."""
    rows = []
    for col in schema_mod.value_columns():
        if col not in df.columns:
            continue
        series = pd.to_numeric(df[col], errors="coerce")
        rows.append({
            "column": col,
            "nulls": int(series.isna().sum()),
            "null_pct": round(100.0 * series.isna().mean(), 2) if len(series) else 0.0,
            "min": round(float(series.min()), 3) if series.notna().any() else None,
            "max": round(float(series.max()), 3) if series.notna().any() else None,
            "mean": round(float(series.mean()), 3) if series.notna().any() else None,
            "std": round(float(series.std()), 4) if series.notna().any() else None,
            "distinct": int(series.nunique(dropna=True)),
        })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------
def run_gate(df):
    """Run every check. Returns (checks, summary)."""
    checks = []
    targets = schema_mod.target_columns()

    # --- 1. volume -----------------------------------------------------
    min_rows = config.get_int("validation.min_rows")
    n = len(df)
    checks.append(_check(
        "row_count",
        PASS if n >= min_rows else FAIL,
        f"{n} rows (minimum {min_rows})",
        n,
    ))

    if n == 0:
        return checks, _summarise(checks, df, 0.0)

    # --- 2. structural conformance to the contract ---------------------
    schema_report = schema_mod.validate(df)
    n_breaches = len(schema_report["breaches"])
    checks.append(_check(
        "schema_contract",
        PASS if n_breaches == 0 else FAIL,
        "conforms" if n_breaches == 0
        else "; ".join(b["detail"] for b in schema_report["breaches"][:4]),
        n_breaches,
    ))

    # --- 3. timestamps -------------------------------------------------
    ts = pd.to_datetime(df["ts"], errors="coerce")
    n_bad_ts = int(ts.isna().sum())
    checks.append(_check(
        "timestamp_parseable",
        PASS if n_bad_ts == 0 else FAIL,
        f"{n_bad_ts} unparseable timestamp(s)",
        n_bad_ts,
    ))

    n_unsorted = int((ts.diff().dt.total_seconds() < 0).sum())
    checks.append(_check(
        "timestamp_monotonic",
        PASS if n_unsorted == 0 else WARN,
        "ordered" if n_unsorted == 0
        else f"{n_unsorted} out-of-order timestamp(s); the pipeline sorts, "
             f"but out-of-order arrival suggests a collector problem",
        n_unsorted,
    ))

    horizon = datetime.now() + timedelta(minutes=5)
    n_future = int((ts > horizon).sum())
    checks.append(_check(
        "timestamp_not_future",
        PASS if n_future == 0 else FAIL,
        f"{n_future} timestamp(s) more than 5 minutes in the future "
        f"(clock skew corrupts every lag feature)",
        n_future,
    ))

    # --- 4. duplicates -------------------------------------------------
    n_dupes = int(ts.duplicated().sum())
    dupe_fraction = n_dupes / n
    max_dupes = config.get_float("validation.max_duplicate_fraction")
    checks.append(_check(
        "duplicate_timestamps",
        PASS if dupe_fraction <= max_dupes else FAIL,
        f"{n_dupes} duplicate timestamp(s) = {dupe_fraction:.2%} "
        f"(limit {max_dupes:.2%})",
        n_dupes,
    ))

    n_dupe_rows = int(df.duplicated().sum())
    checks.append(_check(
        "duplicate_rows",
        PASS if n_dupe_rows == 0 else WARN,
        f"{n_dupe_rows} fully identical row(s)",
        n_dupe_rows,
    ))

    # --- 5. nulls in target columns ------------------------------------
    max_null = config.get_float("validation.max_null_fraction")
    worst_null, worst_col = 0.0, None
    for col in targets:
        if col not in df.columns:
            continue
        fraction = float(pd.to_numeric(df[col], errors="coerce").isna().mean())
        if fraction > worst_null:
            worst_null, worst_col = fraction, col
    checks.append(_check(
        "target_nulls",
        PASS if worst_null <= max_null else FAIL,
        f"worst target null rate {worst_null:.2%}"
        + (f" in {worst_col}" if worst_col else "")
        + f" (limit {max_null:.2%})",
        round(worst_null, 4),
    ))

    # --- 6. business rule: percentages are percentages -----------------
    out_of_range = 0
    for rule in schema_mod.contract():
        col = rule["column"]
        if col not in df.columns or rule["min_value"] is None:
            continue
        series = pd.to_numeric(df[col], errors="coerce")
        below = int((series < rule["min_value"]).sum())
        above = (int((series > rule["max_value"]).sum())
                 if rule["max_value"] is not None else 0)
        out_of_range += below + above
    checks.append(_check(
        "value_ranges",
        PASS if out_of_range == 0 else FAIL,
        f"{out_of_range} value(s) outside the declared range",
        out_of_range,
    ))

    # --- 7. cadence ----------------------------------------------------
    cadence = detect_cadence(df)
    nominal = config.get_int("pipeline.nominal_cadence_sec")
    drifted = abs(cadence - nominal) > 0.5 * nominal
    checks.append(_check(
        "cadence",
        WARN if drifted else PASS,
        f"measured {cadence:.1f}s, configured {nominal}s"
        + (" — the configured value does not describe this data" if drifted else ""),
        round(cadence, 2),
    ))

    # --- 8. collection gaps --------------------------------------------
    gaps = find_gaps(df, cadence)
    total_missed = int(gaps["missed_samples"].sum()) if not gaps.empty else 0
    checks.append(_check(
        "collection_gaps",
        PASS if gaps.empty else WARN,
        "continuous collection" if gaps.empty
        else f"{len(gaps)} gap(s), ~{total_missed} missed sample(s); "
             f"longest {gaps['gap_seconds'].max() / 60:.1f} min. Feature "
             f"windows must not span these.",
        len(gaps),
    ))

    # --- 9. degenerate targets -----------------------------------------
    threshold = config.get_float("pipeline.degenerate_std_threshold")
    flat = []
    for col in targets:
        if col not in df.columns:
            continue
        series = pd.to_numeric(df[col], errors="coerce")
        if series.notna().sum() > 1 and float(series.std()) < threshold:
            flat.append(f"{col} (std={series.std():.4f})")
    checks.append(_check(
        "target_variability",
        PASS if not flat else WARN,
        "all targets vary" if not flat
        else f"effectively constant: {', '.join(flat)}. Forecasting these "
             f"is not meaningful; the baseline is already optimal.",
        len(flat),
    ))

    # --- 10. coverage ---------------------------------------------------
    span_minutes = (ts.max() - ts.min()).total_seconds() / 60.0
    expected = span_minutes * 60.0 / cadence if cadence else 0
    coverage = (n / expected) if expected else 0.0
    checks.append(_check(
        "coverage",
        PASS if coverage >= 0.8 else WARN,
        f"{coverage:.1%} of the samples the span implies "
        f"({n} present, ~{int(expected)} expected over {span_minutes:.1f} min)",
        round(coverage, 3),
    ))

    return checks, _summarise(checks, df, cadence)


def _summarise(checks, df, cadence):
    counts = {PASS: 0, WARN: 0, FAIL: 0}
    for c in checks:
        counts[c["status"]] += 1

    fail_on_warn = config.get_bool("validation.fail_on_warn")
    blocked = counts[FAIL] > 0 or (fail_on_warn and counts[WARN] > 0)

    return {
        "passed": counts[PASS],
        "warned": counts[WARN],
        "failed": counts[FAIL],
        "verdict": FAIL if blocked else (WARN if counts[WARN] else PASS),
        "should_proceed": not blocked,
        "rows": len(df),
        "cadence_seconds": round(cadence, 2),
    }


# ----------------------------------------------------------------------
# Persistence — data health becomes queryable, not just printable
# ----------------------------------------------------------------------
def persist(run_id, checks):
    now = datetime.now().isoformat(timespec="seconds")
    rows = [
        (run_id, c["check"], c["status"],
         float(c["value"]) if isinstance(c["value"], (int, float)) else None,
         c["detail"], now)
        for c in checks
    ]
    for row in rows:
        execute_query(
            """
            INSERT INTO quality_checks
                (run_id, check_name, status, value, detail, checked_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            row,
        )
    return len(rows)


def history(check_name=None, limit=100):
    """Past gate results, for the data-health dashboard."""
    if check_name:
        return execute_query(
            "SELECT run_id, check_name, status, value, detail, checked_at "
            "FROM quality_checks WHERE check_name = ? "
            "ORDER BY id DESC LIMIT ?",
            (check_name, int(limit)), fetch=True,
        ) or []
    return execute_query(
        "SELECT run_id, check_name, status, value, detail, checked_at "
        "FROM quality_checks ORDER BY id DESC LIMIT ?",
        (int(limit),), fetch=True,
    ) or []


def format_report(checks, summary):
    symbols = {PASS: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]"}
    lines = [
        "=" * 78,
        "DATA QUALITY GATE",
        "=" * 78,
    ]
    for c in checks:
        lines.append(f"  {symbols[c['status']]} {c['check']:22s} {c['detail']}")

    lines.append("-" * 78)
    lines.append(
        f"  {summary['passed']} passed, {summary['warned']} warned, "
        f"{summary['failed']} failed -> {summary['verdict']}"
    )
    lines.append(
        "  pipeline may proceed" if summary["should_proceed"]
        else "  PIPELINE BLOCKED — data is not fit to model"
    )
    lines.append("=" * 78)
    return "\n".join(lines)


if __name__ == "__main__":
    from pipeline.sources import get_source

    frame = schema_mod.coerce(get_source("sqlite://").read())
    result, gate_summary = run_gate(frame)
    print(format_report(result, gate_summary))

    gap_table = find_gaps(frame)
    if not gap_table.empty:
        print("\nCollection gaps:")
        print(gap_table.to_string(index=False))

    print("\nColumn profile:")
    print(profile(frame).to_string(index=False))

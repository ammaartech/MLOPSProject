"""
STAGE 2 — Data Engineering: schema management.

The contract lives in the `schema_contract` table, not in this file. This
module reads whatever rules the database holds and enforces them. Adding
a column, tightening a range or making a field non-nullable is a row edit,
not a code change.

Two responsibilities, deliberately separate:

    coerce(df)    make the frame match the declared types, best-effort
    validate(df)  report every place it still does not match

`coerce` is forgiving because raw feeds are messy; `validate` is strict
because the pipeline needs to know what the coercion could not fix. Stage
4 (`validate.py`) then decides whether the breaches are severe enough to
stop the run.
"""

import numpy as np
import pandas as pd

from crud.query import execute_query

# How a declared dtype maps onto a pandas conversion.
_NUMERIC = {"float", "int"}


# ----------------------------------------------------------------------
# The contract
# ----------------------------------------------------------------------
def contract(table="metrics"):
    """Return the declared column rules for `table`, in declaration order."""
    rows = execute_query(
        """
        SELECT column_name, dtype, nullable, min_value, max_value,
               is_target, description
        FROM schema_contract
        WHERE table_name = ?
        """,
        (table,), fetch=True,
    ) or []

    return [
        {
            "column": r[0],
            "dtype": r[1],
            "nullable": bool(r[2]),
            "min_value": r[3],
            "max_value": r[4],
            "is_target": bool(r[5]),
            "description": r[6],
        }
        for r in rows
    ]


def expected_columns(table="metrics"):
    return [rule["column"] for rule in contract(table)]


def target_columns(table="metrics"):
    """Columns marked as forecast targets in the contract."""
    return [rule["column"] for rule in contract(table) if rule["is_target"]]


def value_columns(table="metrics"):
    """Every numeric measurement column (everything but id and ts)."""
    return [
        rule["column"] for rule in contract(table)
        if rule["dtype"] in _NUMERIC and rule["column"] != "id"
    ]


def upsert_rule(table, column, dtype, nullable=True, min_value=None,
                max_value=None, is_target=False, description=None):
    """Add or replace one column rule. This is how the contract evolves."""
    return execute_query(
        """
        INSERT INTO schema_contract
            (table_name, column_name, dtype, nullable,
             min_value, max_value, is_target, description)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(table_name, column_name) DO UPDATE SET
            dtype = excluded.dtype,
            nullable = excluded.nullable,
            min_value = excluded.min_value,
            max_value = excluded.max_value,
            is_target = excluded.is_target,
            description = excluded.description
        """,
        (table, column, dtype, int(nullable), min_value, max_value,
         int(is_target), description),
    )


# ----------------------------------------------------------------------
# Coercion
# ----------------------------------------------------------------------
def coerce(df, table="metrics"):
    """Force `df` to match the declared types where possible.

    Unparseable values become NaN/NaT rather than raising — validate()
    reports them afterwards. Columns absent from the frame are added as
    all-null so downstream code can rely on the shape; columns absent from
    the contract are left untouched.
    """
    out = df.copy()

    for rule in contract(table):
        col, dtype = rule["column"], rule["dtype"]

        if col not in out.columns:
            # Create the column with the RIGHT empty type. A bare pd.NA
            # column lands as object dtype and then trips the dtype check
            # in validate(), reporting a type error for a column whose only
            # problem is that it is absent.
            if dtype in _NUMERIC:
                out[col] = np.nan
            elif dtype == "datetime":
                out[col] = pd.NaT
            else:
                out[col] = pd.Series([pd.NA] * len(out), dtype="string",
                                     index=out.index)
            continue

        if dtype == "datetime":
            out[col] = pd.to_datetime(out[col], errors="coerce")
        elif dtype in _NUMERIC:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        elif dtype == "bool":
            out[col] = out[col].astype("boolean")
        else:
            out[col] = out[col].astype("string")

    return out


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
def validate(df, table="metrics"):
    """Check `df` against the contract. Returns a report dict.

    Reports rather than raises: the caller decides severity. A missing
    optional column and a target column full of nulls are both breaches
    here, but only one of them should stop a pipeline.
    """
    rules = contract(table)
    declared = {r["column"] for r in rules}
    breaches = []

    missing = [c for c in declared if c not in df.columns]
    extra = [c for c in df.columns if c not in declared]

    for col in missing:
        breaches.append({
            "column": col, "kind": "missing_column", "count": len(df),
            "detail": "declared in the contract but absent from the data",
        })

    for rule in rules:
        col = rule["column"]
        if col not in df.columns:
            continue
        series = df[col]

        # --- nullability ---
        n_null = int(series.isna().sum())
        if n_null and not rule["nullable"]:
            breaches.append({
                "column": col, "kind": "null_in_non_nullable", "count": n_null,
                "detail": f"{n_null} null(s) in a column declared NOT NULL",
            })

        # --- type conformance ---
        if rule["dtype"] in _NUMERIC and not pd.api.types.is_numeric_dtype(series):
            breaches.append({
                "column": col, "kind": "dtype_mismatch", "count": len(series),
                "detail": f"declared {rule['dtype']}, found {series.dtype}",
            })
        if rule["dtype"] == "datetime" and not pd.api.types.is_datetime64_any_dtype(series):
            breaches.append({
                "column": col, "kind": "dtype_mismatch", "count": len(series),
                "detail": f"declared datetime, found {series.dtype}",
            })

        # --- declared range ---
        if rule["dtype"] in _NUMERIC and pd.api.types.is_numeric_dtype(series):
            if rule["min_value"] is not None:
                n_low = int((series < rule["min_value"]).sum())
                if n_low:
                    breaches.append({
                        "column": col, "kind": "below_min", "count": n_low,
                        "detail": f"{n_low} value(s) below declared minimum "
                                  f"{rule['min_value']}",
                    })
            if rule["max_value"] is not None:
                n_high = int((series > rule["max_value"]).sum())
                if n_high:
                    breaches.append({
                        "column": col, "kind": "above_max", "count": n_high,
                        "detail": f"{n_high} value(s) above declared maximum "
                                  f"{rule['max_value']}",
                    })

    return {
        "table": table,
        "rows": len(df),
        "columns_seen": len(df.columns),
        "columns_declared": len(rules),
        "missing_columns": missing,
        "undeclared_columns": extra,
        "breaches": breaches,
        "conforms": len(breaches) == 0,
    }


def format_report(report):
    lines = [
        "=" * 68,
        f"SCHEMA CONTRACT — {report['table']}",
        "=" * 68,
        f"  rows              : {report['rows']}",
        f"  columns declared  : {report['columns_declared']}",
        f"  columns seen      : {report['columns_seen']}",
    ]
    if report["undeclared_columns"]:
        lines.append(f"  undeclared columns: {report['undeclared_columns']} "
                     f"(carried through, not validated)")

    if report["conforms"]:
        lines.append("  verdict           : CONFORMS")
    else:
        lines.append(f"  verdict           : {len(report['breaches'])} BREACH(ES)")
        for b in report["breaches"]:
            lines.append(f"    - {b['column']:14s} {b['kind']:22s} {b['detail']}")

    lines.append("=" * 68)
    return "\n".join(lines)


if __name__ == "__main__":
    from pipeline.sources import get_source

    frame = get_source("sqlite://").read()
    frame = coerce(frame)
    print(format_report(validate(frame)))
    print("\nTargets declared in the contract:", target_columns())

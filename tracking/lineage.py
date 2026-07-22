"""
Lineage — answering "where did this number come from?"

Each stage records identifiers rather than just results, and this module
walks the chain backwards:

    prediction
      -> model_id        which model produced it
      -> feature_version which feature definition it consumed
      -> run_id          which ETL execution built that data
      -> data_fingerprint    the content hash of the input rows
      -> config_fingerprint  the settings in force at the time
      -> quality_checks  what the gate said about that data

Why this is worth the bookkeeping
---------------------------------
Three weeks after a demo, the question is never "what is the MAE". It is
"this allocation looks wrong, why did the system decide that". Without
lineage the answer is a shrug and a re-run against data that has since
changed. With it, the exact inputs are recoverable and the config that
produced them is on record — including whether the quality gate had
warned about the data at the time.
"""

import json

import pandas as pd

from crud.query import execute_query


# ----------------------------------------------------------------------
# Walking the chain
# ----------------------------------------------------------------------
def trace_model(model_id):
    """Everything upstream of one model."""
    rows = execute_query(
        """
        SELECT model_id, target, algorithm, params, feature_version,
               data_fingerprint, config_fingerprint, run_id, train_rows,
               test_rows, mae, baseline_mae, is_champion, promoted_at,
               rejected_reason, mlflow_run_id, created_at
        FROM model_versions WHERE model_id = ?
        """,
        (model_id,), fetch=True,
    )
    if not rows:
        return {"error": f"no model '{model_id}'"}

    r = rows[0]
    trace = {
        "model": {
            "model_id": r[0], "target": r[1], "algorithm": r[2],
            "params": json.loads(r[3]) if r[3] else {},
            "train_rows": r[8], "test_rows": r[9],
            "mae": r[10], "baseline_mae": r[11],
            "is_champion": bool(r[12]), "promoted_at": r[13],
            "rejected_reason": r[14], "mlflow_run_id": r[15],
            "created_at": r[16],
        },
        "feature_version": r[4],
        "data_fingerprint": r[5],
        "config_fingerprint": r[6],
        "etl_run_id": r[7],
    }

    # --- the feature definition -----------------------------------------
    if trace["feature_version"]:
        feature_rows = execute_query(
            "SELECT feature_list, n_features, n_rows, created_at "
            "FROM feature_versions WHERE version_id = ?",
            (trace["feature_version"],), fetch=True,
        )
        if feature_rows:
            f = feature_rows[0]
            trace["features"] = {
                "version_id": trace["feature_version"],
                "columns": json.loads(f[0]),
                "n_features": f[1], "n_rows": f[2], "created_at": f[3],
            }

    # --- the ETL run that produced the data ------------------------------
    if trace["etl_run_id"] is not None:
        run_rows = execute_query(
            """
            SELECT run_id, started_at, finished_at, source_kind, source_detail,
                   rows_in, rows_out, gate_verdict, gate_detail,
                   data_fingerprint, config_fingerprint, status
            FROM pipeline_runs WHERE run_id = ?
            """,
            (trace["etl_run_id"],), fetch=True,
        )
        if run_rows:
            r = run_rows[0]
            trace["etl_run"] = {
                "run_id": r[0], "started_at": r[1], "finished_at": r[2],
                "source": f"{r[3]} ({r[4]})",
                "rows_in": r[5], "rows_out": r[6],
                "gate_verdict": r[7], "gate_detail": r[8],
                "data_fingerprint": r[9], "config_fingerprint": r[10],
                "status": r[11],
            }

        checks = execute_query(
            "SELECT check_name, status, detail FROM quality_checks "
            "WHERE run_id = ? ORDER BY id",
            (trace["etl_run_id"],), fetch=True,
        ) or []
        trace["quality_checks"] = [
            {"check": c[0], "status": c[1], "detail": c[2]} for c in checks
        ]

    return trace


def trace_prediction(prediction_id=None, target=None, ts=None):
    """From a logged prediction back to the model and its inputs."""
    if prediction_id is not None:
        rows = execute_query(
            "SELECT id, model_id, target, predicted_for_ts, predicted, "
            "actual, abs_error, created_at FROM predictions WHERE id = ?",
            (prediction_id,), fetch=True,
        )
    else:
        rows = execute_query(
            "SELECT id, model_id, target, predicted_for_ts, predicted, "
            "actual, abs_error, created_at FROM predictions "
            "WHERE target = ? AND predicted_for_ts = ?",
            (target, ts), fetch=True,
        )
    if not rows:
        return {"error": "no matching prediction"}

    r = rows[0]
    prediction = {
        "prediction_id": r[0], "model_id": r[1], "target": r[2],
        "predicted_for_ts": r[3], "predicted": r[4],
        "actual": r[5], "abs_error": r[6], "created_at": r[7],
    }
    return {"prediction": prediction, **trace_model(prediction["model_id"])}


def config_at(fingerprint):
    """Which config values a fingerprint corresponded to.

    Reconstructed from `config_history`: the current values, rolled back
    through every change recorded since. Exact when the audit trail is
    complete, which it is for anything the running system changed.
    """
    import config as config_mod

    current = config_mod.all_values()
    if config_mod.fingerprint() == fingerprint:
        return {"exact": True, "values": current}

    history = execute_query(
        "SELECT key, old_value, new_value, changed_at FROM config_history "
        "ORDER BY id DESC", fetch=True,
    ) or []
    return {
        "exact": False,
        "values": current,
        "note": (f"current config is {config_mod.fingerprint()}, not "
                 f"{fingerprint}; {len(history)} recorded change(s) since"),
        "changes": [
            {"key": h[0], "from": h[1], "to": h[2], "at": h[3]}
            for h in history[:20]
        ],
    }


# ----------------------------------------------------------------------
# Overview
# ----------------------------------------------------------------------
def champions():
    rows = execute_query(
        """
        SELECT target, model_id, algorithm, mae, feature_version,
               data_fingerprint, run_id, promoted_at
        FROM model_versions WHERE is_champion = 1 ORDER BY target
        """, fetch=True,
    ) or []
    return pd.DataFrame(rows, columns=[
        "target", "model_id", "algorithm", "mae", "feature_version",
        "data_fingerprint", "run_id", "promoted_at",
    ])


def format_trace(trace):
    if "error" in trace:
        return f"  {trace['error']}"

    m = trace["model"]
    lines = [
        "=" * 80,
        f"LINEAGE — {m['model_id']}",
        "=" * 80,
        "  MODEL",
        f"    target            : {m['target']}",
        f"    algorithm         : {m['algorithm']}",
        f"    MAE / baseline    : {m['mae']} / {m['baseline_mae']}",
        f"    champion          : {m['is_champion']}"
        + (f" (promoted {m['promoted_at']})" if m["is_champion"] else ""),
    ]
    if m["rejected_reason"]:
        lines.append(f"    rejected          : {m['rejected_reason'][:70]}")
    if m["mlflow_run_id"]:
        lines.append(f"    mlflow run        : {m['mlflow_run_id']}")

    if "features" in trace:
        f = trace["features"]
        lines += [
            "  FEATURES",
            f"    version           : {f['version_id']}",
            f"    columns           : {f['n_features']} "
            f"({', '.join(f['columns'][:4])}, ...)",
            f"    materialised rows : {f['n_rows']}",
        ]

    if "etl_run" in trace:
        e = trace["etl_run"]
        lines += [
            "  DATA",
            f"    etl run           : {e['run_id']} ({e['status']})",
            f"    source            : {e['source']}",
            f"    rows in / out     : {e['rows_in']} -> {e['rows_out']}",
            f"    quality gate      : {e['gate_verdict']} ({e['gate_detail']})",
            f"    data fingerprint  : {e['data_fingerprint']}",
            f"    config fingerprint: {e['config_fingerprint']}",
        ]

    if trace.get("quality_checks"):
        warned = [c for c in trace["quality_checks"] if c["status"] != "PASS"]
        lines.append(f"  QUALITY WARNINGS AT TRAINING TIME ({len(warned)})")
        for c in warned:
            lines.append(f"    [{c['status']}] {c['check']}: {c['detail'][:56]}")

    lines.append("=" * 80)
    return "\n".join(lines)


if __name__ == "__main__":
    table = champions()
    if table.empty:
        raise SystemExit("No champions yet. Run: python -m tracking.mlflow_tracker")

    print("CHAMPIONS SERVING PRODUCTION")
    print(table.to_string(index=False))
    print()

    for model_id in table["model_id"]:
        print(format_trace(trace_model(model_id)))
        print()

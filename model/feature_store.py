"""
STAGE 10 — Feature Store: offline and online stores, versioning.

Two stores over one definition.

    OFFLINE   `feature_versions` + `feature_values`
              Every materialised training row, addressed by version id.
              Answers "what exactly did this model train on?" months
              later, when the config has moved on.

    ONLINE    `feature_online`
              One row per target: the current feature vector, ready for
              inference without recomputing history.

What a version id is
--------------------
A hash of three things:

    the feature column list
    the config keys that change what a feature VALUE means
    the fingerprint of the data it was built from

So changing `features.lags` produces a new version, and changing a cloud
price does not — pricing never entered a feature. `features.defining_keys`
in the config table decides which keys count, and is itself editable.

The problem this exists to prevent
----------------------------------
Training-serving skew: a model trained on features computed one way,
served features computed another. Here both paths call
`model.features.build_feature_frame`, and the online store records the
version id it was written under. `predictor.py` refuses to serve when the
champion's feature version does not match the online vector's, which
turns a silent wrong answer into a loud refusal.
"""

import json
from datetime import datetime

import pandas as pd

import config
from crud.query import execute_many, execute_query
from model.features import build_features, build_serving_row


# ----------------------------------------------------------------------
# Versioning
# ----------------------------------------------------------------------
def version_id(target, feature_columns, data_fingerprint=None):
    """Stable short id for one (target, feature definition, data) triple.

    This identifies a MATERIALISATION — one specific training set, built
    from one specific snapshot of the data. It is the right key for the
    offline store, where "which rows" is exactly the question.

    It is the wrong key for a train/serve compatibility check. See
    `definition_id`.
    """
    import hashlib

    payload = json.dumps({
        "target": target,
        "features": sorted(feature_columns),
        "config": config.feature_fingerprint(),
        "data": data_fingerprint or "",
    }, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def definition_id(target, feature_columns):
    """Stable short id for what a feature vector MEANS, ignoring the data.

    Why this is separate from `version_id`
    --------------------------------------
    The serving path refuses to predict when the champion's features do
    not match the ones the online store holds, which is right: a model
    fed a differently-shaped or differently-defined vector produces a
    number that looks fine and means nothing.

    That check used to compare `version_id`, which hashes the data
    fingerprint along with the definition. The data fingerprint changes
    every time the collector appends a row — so a trained champion became
    unservable within seconds of being promoted, and stayed that way
    until it was retrained AND re-promoted on the newest snapshot. The
    symptom was `cpu_percent  n/a - feature version mismatch` on every
    cycle, with the advice "re-run the feature store", which could never
    resolve it: refreshing the store produced yet another new data
    fingerprint.

    Train/serve skew is a question about DEFINITIONS — the feature list,
    the lag depths, the rolling window, the target shift. Those are the
    target, the column set, and `config.feature_fingerprint()`, and none
    of them move when a sample arrives. That is what this hashes.
    """
    import hashlib

    payload = json.dumps({
        "target": target,
        "config": config.feature_fingerprint(),
    }, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


# ----------------------------------------------------------------------
# Offline store
# ----------------------------------------------------------------------
def materialise(X, y, meta, target, run_id=None, data_fingerprint=None):
    """Write a training set to the offline store. Returns the manifest."""
    if X.empty:
        return {"error": "nothing to materialise", "target": target}

    columns = list(X.columns)
    version = version_id(target, columns, data_fingerprint)
    definition = definition_id(target, columns)

    execute_query(
        """
        INSERT OR REPLACE INTO feature_versions
            (version_id, target, feature_list, n_features, n_rows,
             config_fingerprint, data_fingerprint, run_id, created_at,
             definition_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (version, target, json.dumps(columns), len(columns), len(X),
         config.feature_fingerprint(), data_fingerprint, run_id,
         datetime.now().isoformat(timespec="seconds"), definition),
    )

    timestamps = meta.get("timestamps")
    segments = meta.get("segment_ids")
    rows = [
        (
            version,
            (timestamps.iloc[i].isoformat()
             if timestamps is not None else str(i)),
            int(segments.iloc[i]) if segments is not None else 0,
            json.dumps({c: _clean(X.iloc[i][c]) for c in columns}),
            _clean(y.iloc[i]),
        )
        for i in range(len(X))
    ]

    # Replace any previous materialisation of this exact version so a
    # re-run cannot double the row count.
    execute_query("DELETE FROM feature_values WHERE version_id = ?", (version,))
    execute_many(
        """
        INSERT OR REPLACE INTO feature_values
            (version_id, ts, segment_id, features, target_value)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )

    return {
        "version_id": version,
        "definition_id": definition,
        "target": target,
        "n_features": len(columns),
        "n_rows": len(X),
        "feature_columns": columns,
        "config_fingerprint": config.feature_fingerprint(),
        "data_fingerprint": data_fingerprint,
    }


def load_offline(version=None, target=None):
    """Read a materialised training set back. Returns (X, y, manifest)."""
    if version is None:
        if target is None:
            raise ValueError("pass either a version id or a target")
        version = latest_version(target)
        if version is None:
            return pd.DataFrame(), pd.Series(dtype=float), {}

    manifest = get_manifest(version)
    if not manifest:
        return pd.DataFrame(), pd.Series(dtype=float), {}

    rows = execute_query(
        "SELECT ts, segment_id, features, target_value FROM feature_values "
        "WHERE version_id = ? ORDER BY ts ASC",
        (version,), fetch=True,
    ) or []
    if not rows:
        return pd.DataFrame(), pd.Series(dtype=float), manifest

    columns = manifest["feature_columns"]
    X = pd.DataFrame([json.loads(r[2]) for r in rows])[columns]
    y = pd.Series([r[3] for r in rows], name="__target__")
    manifest["timestamps"] = pd.to_datetime([r[0] for r in rows])
    return X, y, manifest


def get_manifest(version):
    rows = execute_query(
        """
        SELECT version_id, target, feature_list, n_features, n_rows,
               config_fingerprint, data_fingerprint, run_id, created_at
        FROM feature_versions WHERE version_id = ?
        """,
        (version,), fetch=True,
    )
    if not rows:
        return {}
    r = rows[0]
    return {
        "version_id": r[0], "target": r[1],
        "feature_columns": json.loads(r[2]),
        "n_features": r[3], "n_rows": r[4],
        "config_fingerprint": r[5], "data_fingerprint": r[6],
        "run_id": r[7], "created_at": r[8],
    }


def latest_version(target):
    rows = execute_query(
        "SELECT version_id FROM feature_versions WHERE target = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (target,), fetch=True,
    )
    return rows[0][0] if rows else None


def list_versions(target=None):
    if target:
        rows = execute_query(
            "SELECT version_id, target, n_features, n_rows, "
            "config_fingerprint, data_fingerprint, created_at "
            "FROM feature_versions WHERE target = ? ORDER BY created_at DESC",
            (target,), fetch=True,
        )
    else:
        rows = execute_query(
            "SELECT version_id, target, n_features, n_rows, "
            "config_fingerprint, data_fingerprint, created_at "
            "FROM feature_versions ORDER BY created_at DESC", fetch=True,
        )
    return pd.DataFrame(rows or [], columns=[
        "version_id", "target", "n_features", "n_rows",
        "config_fp", "data_fp", "created_at",
    ])


# ----------------------------------------------------------------------
# Online store
# ----------------------------------------------------------------------
def write_online(target, feature_row, version, ts, definition=None):
    """Publish the current feature vector for `target`.

    Both ids are stored. `version_id` says which data snapshot this vector
    was built alongside — useful for tracing. `definition_id` says what
    the vector MEANS, and is the one the serving compatibility check
    compares, because it does not move when a sample arrives.
    """
    values = {c: _clean(feature_row.iloc[0][c]) for c in feature_row.columns}
    if definition is None:
        definition = definition_id(target, list(feature_row.columns))
    execute_query(
        """
        INSERT OR REPLACE INTO feature_online
            (target, version_id, ts, features, updated_at, definition_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (target, version,
         ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
         json.dumps(values),
         datetime.now().isoformat(timespec="seconds"), definition),
    )
    return {"target": target, "version_id": version,
            "definition_id": definition, "ts": str(ts),
            "n_features": len(values)}


def get_online_features(target):
    """The current feature vector for inference. Returns (frame, meta)."""
    rows = execute_query(
        "SELECT version_id, ts, features, updated_at, definition_id "
        "FROM feature_online WHERE target = ?",
        (target,), fetch=True,
    )
    if not rows:
        return None, {"error": f"no online features for {target}; "
                               f"run refresh_online()"}
    version, ts, payload, updated, definition = rows[0]
    values = json.loads(payload)
    # A row written before `definition_id` existed has NULL there. Derive
    # it from the columns actually present rather than reporting None,
    # which the serving check would read as "cannot compare".
    if not definition:
        definition = definition_id(target, list(values))
    return pd.DataFrame([values]), {
        "version_id": version, "definition_id": definition,
        "ts": ts, "updated_at": updated,
        "feature_columns": list(values),
    }


def refresh_online(df, target_list=None, data_fingerprint=None):
    """Recompute and publish the current vector for every target."""
    published = []
    for target in (target_list or config.get_json("features.targets")):
        row, info = build_serving_row(df, target)
        if row is None:
            published.append({"target": target, "error": info.get("error")})
            continue
        columns = list(row.columns)
        published.append(write_online(
            target, row,
            version_id(target, columns, data_fingerprint), info["ts"],
            definition=definition_id(target, columns),
        ))
    return published


# ----------------------------------------------------------------------
# Point-in-time join
# ----------------------------------------------------------------------
def get_historical_features(entity_df, target, version=None, feature_names=None):
    """Join features onto timestamps AS THEY WERE at each timestamp.

    The join is on the exact stored timestamp rather than a nearest match.
    A tolerant join would happily attach a feature computed after the
    moment being scored, which is the classic way a backtest produces a
    result that cannot be reproduced live.
    """
    X, y, manifest = load_offline(version=version, target=target)
    if X.empty:
        return pd.DataFrame()

    stored = X.copy()
    stored["ts"] = manifest["timestamps"]
    stored["__target__"] = y.values

    wanted = entity_df.copy()
    wanted["ts"] = pd.to_datetime(wanted["ts"])

    joined = wanted.merge(stored, on="ts", how="left")
    if feature_names:
        keep = ["ts"] + [c for c in feature_names if c in joined.columns]
        joined = joined[keep + (["__target__"] if "__target__" in joined else [])]
    return joined


# ----------------------------------------------------------------------
def materialise_all(df, run_id=None, data_fingerprint=None):
    """Materialise every target from a cleaned frame."""
    manifests = {}
    for target in config.get_json("features.targets"):
        X, y, meta = build_features(df, target)
        manifests[target] = materialise(
            X, y, meta, target, run_id=run_id, data_fingerprint=data_fingerprint
        )
    return manifests


def _clean(value):
    """JSON cannot hold NaN or numpy scalars."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return float(value) if hasattr(value, "item") or isinstance(value, (int, float)) else value


def format_report(manifests, published=None):
    lines = ["=" * 80, "FEATURE STORE", "=" * 80, "  OFFLINE"]
    for target, m in manifests.items():
        if "error" in m:
            lines.append(f"    {target:14s} {m['error']}")
            continue
        lines.append(
            f"    {target:14s} version={m['version_id']}  "
            f"rows={m['n_rows']:<5} features={m['n_features']:<3} "
            f"config_fp={m['config_fingerprint']}"
        )
    if published:
        lines.append("  ONLINE")
        for p in published:
            if "error" in p:
                lines.append(f"    {p['target']:14s} {p['error']}")
            else:
                lines.append(
                    f"    {p['target']:14s} version={p['version_id']}  "
                    f"ts={p['ts']}  features={p['n_features']}"
                )
    lines.append("=" * 80)
    return "\n".join(lines)


if __name__ == "__main__":
    from model.features import load_clean_frame
    from pipeline.etl import latest_run

    frame = load_clean_frame()
    if frame.empty:
        raise SystemExit("No cleaned data. Run: python -m pipeline.etl")

    run = latest_run() or {}
    manifests = materialise_all(frame, run_id=run.get("run_id"),
                                data_fingerprint=run.get("data_fingerprint"))
    published = refresh_online(frame, data_fingerprint=run.get("data_fingerprint"))
    print(format_report(manifests, published))

    print("\nVersions on record:")
    print(list_versions().to_string(index=False))

    example = config.get_json("features.targets")[0]
    X, y, manifest = load_offline(target=example)
    print(f"\nRound-trip {example}: {len(X)} rows x {len(X.columns)} features "
          f"from version {manifest['version_id']}")

    entities = pd.DataFrame({"ts": manifest["timestamps"][:5]})
    joined = get_historical_features(entities, example)
    print(f"Point-in-time join: {len(entities)} entities -> "
          f"{joined['__target__'].notna().sum()} matched")

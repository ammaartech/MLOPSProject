"""
STAGE 1 — Data Sources.

Everything downstream reads through the `Source` interface, so where the
data came from is a configuration choice rather than something baked into
the pipeline. Four connectors are implemented:

    sqlite://              the live metrics table (the normal path)
    csv://<path>           a file export, for replay and for sharing
    http://<url>           a JSON endpoint, for a remote agent or a fleet
    stream://              incremental reads, only rows newer than a
                           watermark — how a continuously running
                           collector is consumed without reprocessing

The honest scope note: this is a real, pluggable interface with four
working connectors, not five live cloud integrations. `HTTPSource` will
talk to any endpoint returning a list of JSON records; nothing in the
project currently serves one. That is the point of the interface — adding
an S3 or Kafka connector means writing one class here and changing a
config value, with no change anywhere downstream.
"""

import json
import os
import urllib.request
from datetime import datetime

import pandas as pd

import config
from crud.query import execute_query
from pipeline import schema as schema_mod

DEFAULT_CSV = os.path.join(config.DATA_DIR, "metrics_export.csv")
WATERMARK_PATH = os.path.join(config.DATA_DIR, "stream_watermark.json")


# ----------------------------------------------------------------------
# Interface
# ----------------------------------------------------------------------
class Source:
    """A named, describable producer of raw metric rows."""

    kind = "abstract"

    def read(self):
        """Return a DataFrame of raw rows, oldest first."""
        raise NotImplementedError

    def describe(self):
        """Where this data came from, for the lineage record."""
        return {"kind": self.kind, "detail": ""}

    # Shared by every concrete source so they all return the same shape.
    def _finalise(self, df):
        if df.empty:
            return df
        for col in schema_mod.expected_columns():
            if col not in df.columns:
                df[col] = pd.NA
        df = df[[c for c in schema_mod.expected_columns() if c in df.columns]
                + [c for c in df.columns if c not in schema_mod.expected_columns()]]
        if "ts" in df.columns:
            df = df.sort_values("ts").reset_index(drop=True)
        return df


# ----------------------------------------------------------------------
# Databases
# ----------------------------------------------------------------------
class SQLiteSource(Source):
    """The project's own metrics table. The default path."""

    kind = "sqlite"

    def __init__(self, table="metrics", since=None, until=None, limit=None):
        self.table = table
        self.since = since
        self.until = until
        self.limit = limit

    def read(self):
        # The table name is not user input — it comes from the contract —
        # but every VALUE is still bound, never interpolated.
        clauses, values = [], []
        if self.since:
            clauses.append("ts >= ?")
            values.append(self.since)
        if self.until:
            clauses.append("ts <= ?")
            values.append(self.until)

        sql = f"SELECT * FROM {self.table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts ASC"
        if self.limit:
            sql += " LIMIT ?"
            values.append(int(self.limit))

        rows = execute_query(sql, tuple(values) or None, fetch=True) or []
        columns = _table_columns(self.table)
        return self._finalise(pd.DataFrame(rows, columns=columns))

    def describe(self):
        window = f"{self.since or 'start'} .. {self.until or 'now'}"
        return {"kind": self.kind, "detail": f"{config.DB_PATH}:{self.table} [{window}]"}


class StreamSource(Source):
    """Incremental consumption: only rows newer than the last watermark.

    A continuously running collector appends forever. Reprocessing the
    whole table on every cycle gets slower without bound, so the streaming
    path remembers where it stopped. `peek()` reads without advancing,
    which is what the serving loop wants when it only needs the tail.
    """

    kind = "stream"

    def __init__(self, table="metrics", watermark_path=WATERMARK_PATH,
                 advance=True):
        self.table = table
        self.watermark_path = watermark_path
        self.advance = advance

    def _load_watermark(self):
        if not os.path.exists(self.watermark_path):
            return None
        try:
            with open(self.watermark_path) as fh:
                return json.load(fh).get("last_ts")
        except (OSError, ValueError):
            return None

    def _save_watermark(self, last_ts):
        with open(self.watermark_path, "w") as fh:
            json.dump(
                {"last_ts": last_ts, "updated_at": datetime.now().isoformat()},
                fh, indent=2,
            )

    def read(self):
        last = self._load_watermark()
        if last:
            rows = execute_query(
                f"SELECT * FROM {self.table} WHERE ts > ? ORDER BY ts ASC",
                (last,), fetch=True,
            ) or []
        else:
            rows = execute_query(
                f"SELECT * FROM {self.table} ORDER BY ts ASC", fetch=True
            ) or []

        df = self._finalise(pd.DataFrame(rows, columns=_table_columns(self.table)))
        if self.advance and not df.empty:
            self._save_watermark(str(df["ts"].iloc[-1]))
        return df

    def peek(self, n):
        """The most recent `n` rows, without moving the watermark."""
        rows = execute_query(
            f"SELECT * FROM {self.table} ORDER BY ts DESC LIMIT ?",
            (int(n),), fetch=True,
        ) or []
        df = pd.DataFrame(rows, columns=_table_columns(self.table))
        return self._finalise(df.iloc[::-1].reset_index(drop=True))

    def reset(self):
        if os.path.exists(self.watermark_path):
            os.remove(self.watermark_path)

    def describe(self):
        return {"kind": self.kind,
                "detail": f"{self.table} since watermark {self._load_watermark()}"}


# ----------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------
class CSVSource(Source):
    kind = "csv"

    def __init__(self, path=DEFAULT_CSV):
        self.path = path

    def read(self):
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"CSV source not found: {self.path}")
        return self._finalise(pd.read_csv(self.path))

    def describe(self):
        return {"kind": self.kind, "detail": self.path}


# ----------------------------------------------------------------------
# APIs
# ----------------------------------------------------------------------
class HTTPSource(Source):
    """A JSON endpoint returning a list of records, or {"data": [...]}.

    This is the connector a fleet deployment would use: each host runs a
    collector exposing its samples, and this pulls them. Timeouts are
    bounded so a dead endpoint cannot hang the pipeline.
    """

    kind = "http"

    def __init__(self, url, timeout=10, records_key="data"):
        self.url = url
        self.timeout = timeout
        self.records_key = records_key

    def read(self):
        with urllib.request.urlopen(self.url, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode())

        if isinstance(payload, dict):
            payload = payload.get(self.records_key, [])
        if not isinstance(payload, list):
            raise ValueError(
                f"HTTP source {self.url} returned {type(payload).__name__}, "
                f"expected a list of records"
            )
        return self._finalise(pd.DataFrame(payload))

    def describe(self):
        return {"kind": self.kind, "detail": self.url}


# ----------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------
_SCHEMES = {
    "sqlite": SQLiteSource,
    "stream": StreamSource,
    "csv": CSVSource,
    "http": HTTPSource,
    "https": HTTPSource,
}


def get_source(spec=None, **kwargs):
    """Build a Source from a URI-style spec.

        get_source("sqlite://")
        get_source("csv://data/metrics_export.csv")
        get_source("http://10.0.0.4:8000/metrics")
        get_source("stream://")

    With no argument, reads `pipeline.default_source` from config, so the
    running system's input can be changed without editing code.
    """
    if spec is None:
        spec = config.get_str("pipeline.default_source", "sqlite://")

    if "://" not in spec:
        raise ValueError(
            f"source spec '{spec}' must look like '<scheme>://<detail>'; "
            f"known schemes: {sorted(_SCHEMES)}"
        )

    scheme, detail = spec.split("://", 1)
    scheme = scheme.lower()
    if scheme not in _SCHEMES:
        raise ValueError(
            f"unknown source scheme '{scheme}'; known: {sorted(_SCHEMES)}"
        )

    cls = _SCHEMES[scheme]
    if cls is HTTPSource:
        return cls(spec, **kwargs)
    if cls is CSVSource and detail:
        return cls(detail, **kwargs)
    return cls(**kwargs)


def export_csv(path=DEFAULT_CSV, source=None):
    """Write the current metrics table out as CSV (stage 1 in reverse)."""
    df = (source or SQLiteSource()).read()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)
    return {"path": path, "rows": len(df)}


def describe_available():
    """What each connector can currently see. Used by the pipeline report."""
    out = []

    db_rows = execute_query("SELECT COUNT(*) FROM metrics", fetch=True)
    out.append({
        "scheme": "sqlite://", "status": "ready",
        "detail": f"{db_rows[0][0] if db_rows else 0} rows in metrics",
    })

    stream = StreamSource(advance=False)
    out.append({
        "scheme": "stream://", "status": "ready",
        "detail": f"watermark = {stream._load_watermark() or 'unset (full replay)'}",
    })

    out.append({
        "scheme": "csv://", "status": "ready" if os.path.exists(DEFAULT_CSV) else "no export yet",
        "detail": DEFAULT_CSV,
    })

    out.append({
        "scheme": "http://", "status": "implemented, no endpoint configured",
        "detail": "point at any JSON endpoint returning a list of records",
    })
    return out


# ----------------------------------------------------------------------
def _table_columns(table):
    """Column names as the database actually has them."""
    info = execute_query(f"PRAGMA table_info({table})", fetch=True) or []
    return [row[1] for row in info]


if __name__ == "__main__":
    print("Available sources:\n")
    for entry in describe_available():
        print(f"  {entry['scheme']:12s} {entry['status']:34s} {entry['detail']}")

    print("\nReading sqlite:// ...")
    frame = get_source("sqlite://").read()
    print(f"  {len(frame)} rows, columns: {list(frame.columns)}")
    if not frame.empty:
        print(f"  span: {frame['ts'].iloc[0]} .. {frame['ts'].iloc[-1]}")

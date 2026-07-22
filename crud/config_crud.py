"""
CRUD for the `config` table.

This is the storage layer only — raw strings in, raw strings out. Type
coercion and caching live in `config.py`, which is what application code
should import.

Every write is recorded in `config_history`, so a configuration change is
auditable and a model can be traced back to the settings that produced it.
"""

import hashlib
import json

from crud.query import execute_query

_VALUE_TYPES = {"int", "float", "str", "bool", "json"}


# ---------- READ ----------
def get_raw(key):
    """Return (value, value_type) for `key`, or None if it does not exist."""
    rows = execute_query(
        "SELECT value, value_type FROM config WHERE key = ?", (key,), fetch=True
    )
    return (rows[0][0], rows[0][1]) if rows else None


def get_all_raw(category=None):
    """Return {key: (value, value_type)} for every key, or one category."""
    if category:
        rows = execute_query(
            "SELECT key, value, value_type FROM config WHERE category = ? "
            "ORDER BY key",
            (category,), fetch=True,
        )
    else:
        rows = execute_query(
            "SELECT key, value, value_type FROM config ORDER BY key", fetch=True
        )
    return {r[0]: (r[1], r[2]) for r in (rows or [])}


def describe(category=None):
    """Full rows for display: key, value, type, category, description."""
    if category:
        return execute_query(
            "SELECT key, value, value_type, category, description, updated_at "
            "FROM config WHERE category = ? ORDER BY key",
            (category,), fetch=True,
        ) or []
    return execute_query(
        "SELECT key, value, value_type, category, description, updated_at "
        "FROM config ORDER BY category, key", fetch=True
    ) or []


def categories():
    rows = execute_query(
        "SELECT DISTINCT category FROM config ORDER BY category", fetch=True
    )
    return [r[0] for r in (rows or [])]


def exists(key):
    return get_raw(key) is not None


# ---------- CREATE / UPDATE ----------
def set_value(key, value, value_type=None, category=None, description=None,
              source="api"):
    """Insert or update `key`, recording the change in `config_history`.

    `value` may be a Python object; it is serialised according to
    `value_type` (inferred from the existing row when omitted).
    """
    current = get_raw(key)

    if value_type is None:
        if current is None:
            raise KeyError(
                f"config key '{key}' does not exist; pass value_type to create it"
            )
        value_type = current[1]

    if value_type not in _VALUE_TYPES:
        raise ValueError(
            f"unknown value_type '{value_type}'; expected one of {sorted(_VALUE_TYPES)}"
        )

    serialised = serialise(value, value_type)
    old_value = current[0] if current else None

    if current is None:
        execute_query(
            """
            INSERT INTO config
                (key, value, value_type, category, description, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            (key, serialised, value_type, category, description),
        )
    else:
        execute_query(
            "UPDATE config SET value = ?, updated_at = datetime('now') "
            "WHERE key = ?",
            (serialised, key),
        )

    if old_value != serialised:
        execute_query(
            """
            INSERT INTO config_history (key, old_value, new_value, changed_at, source)
            VALUES (?, ?, ?, datetime('now'), ?)
            """,
            (key, old_value, serialised, source),
        )
    return serialised


# ---------- DELETE ----------
def delete_value(key):
    """Remove a key. The next seed will restore it if it has a default."""
    return execute_query("DELETE FROM config WHERE key = ?", (key,))


# ---------- HISTORY ----------
def history(key=None, limit=50):
    if key:
        return execute_query(
            "SELECT key, old_value, new_value, changed_at, source "
            "FROM config_history WHERE key = ? ORDER BY id DESC LIMIT ?",
            (key, int(limit)), fetch=True,
        ) or []
    return execute_query(
        "SELECT key, old_value, new_value, changed_at, source "
        "FROM config_history ORDER BY id DESC LIMIT ?",
        (int(limit),), fetch=True,
    ) or []


# ---------- SERIALISATION ----------
def serialise(value, value_type):
    """Python object -> the TEXT stored in the table."""
    if value_type == "json":
        return value if isinstance(value, str) else json.dumps(value)
    if value_type == "bool":
        if isinstance(value, str):
            return "true" if value.strip().lower() in ("true", "1", "yes", "on") else "false"
        return "true" if value else "false"
    return str(value)


def deserialise(value, value_type):
    """The TEXT stored in the table -> a Python object."""
    if value_type == "int":
        return int(float(value))       # tolerate "20.0" for an int key
    if value_type == "float":
        return float(value)
    if value_type == "bool":
        return value.strip().lower() in ("true", "1", "yes", "on")
    if value_type == "json":
        return json.loads(value)
    return value


# ---------- FINGERPRINT ----------
def fingerprint(keys=None):
    """Stable short hash of the configuration.

    Recorded alongside every trained model and every pipeline run, so a
    result can be tied to the exact settings that produced it. Passing
    `keys` narrows the hash to a subset — the feature store uses this to
    version on only the values that change what a feature means.
    """
    raw = get_all_raw()
    if keys is not None:
        raw = {k: v for k, v in raw.items() if k in set(keys)}
    payload = json.dumps(
        {k: raw[k][0] for k in sorted(raw)}, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]

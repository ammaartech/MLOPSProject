"""
Configuration access.

This module used to be a list of constants. It is now a typed, cached
reader over the `config` table, so every tunable value in the system —
prices, node capacity, thresholds, lag depths, SLA targets, hyperparameters
— is owned by the database and changeable without touching code.

    from config import get_float, get_json

    headroom = get_float("policy.headroom")
    lags     = get_json("features.lags")

Defaults are declared in `database/schema.py` and seeded on first
connection. Editing that file changes what a FRESH database starts with;
it does not change a database that already exists. To change a running
system, write to the table:

    from config import set_value
    set_value("policy.headroom", 0.10)

Bootstrap exception
-------------------
Two things cannot come from the database, because they are needed to open
it: the database path and the directories around it. They are resolved
from the environment, falling back to a path next to this file. That is
the only hardcoded configuration in the project, and it is deliberate.

Why the database sits in `dataset/` and not `data/`
---------------------------------------------------
`data/` is regenerable output — mlflow.db, the serialised models, the CSV
exports — and it is gitignored. `metrics.db` is not regenerable: it is the
collected evidence, and re-running the pipeline cannot bring back samples
that were never taken. It therefore lives in its own tracked directory and
is committed like source.
"""

import os

# ----------------------------------------------------------------------
# Bootstrap — the only values not read from the database.
# ----------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.environ.get("RESOURCE_MONITOR_DATA", os.path.join(BASE_DIR, "data"))
DB_DIR = os.environ.get("RESOURCE_MONITOR_DB_DIR", os.path.join(BASE_DIR, "dataset"))
DB_PATH = os.environ.get("RESOURCE_MONITOR_DB", os.path.join(DB_DIR, "metrics.db"))
MODEL_DIR = os.environ.get("RESOURCE_MONITOR_MODELS", os.path.join(DATA_DIR, "models"))
MLFLOW_URI = os.environ.get(
    "RESOURCE_MONITOR_MLFLOW", "sqlite:///" + os.path.join(DATA_DIR, "mlflow.db")
)

# DB_PATH may be overridden to somewhere outside DB_DIR — k8s points it at
# a per-namespace volume — so create the directory it actually names.
for _d in (DATA_DIR, MODEL_DIR, os.path.dirname(os.path.abspath(DB_PATH))):
    os.makedirs(_d, exist_ok=True)


# ----------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------
# Config is read constantly — every feature build, every cost calculation.
# Without a cache each read is a fresh connection. Writes go through
# set_value(), which invalidates, so the cache cannot go stale from
# inside the process. A long-running process that must observe an
# external edit should call invalidate().
_cache = {}


def invalidate(key=None):
    """Drop the cached value for `key`, or the whole cache."""
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)


def _crud():
    # Imported lazily: crud -> database.connection -> config would be a
    # circular import at module load time. By the time any getter runs,
    # the import graph is settled.
    from crud import config_crud
    return config_crud


# ----------------------------------------------------------------------
# Typed readers
# ----------------------------------------------------------------------
_MISSING = object()


def get(key, default=_MISSING):
    """Read `key`, coerced to the type declared in its row.

    Raises KeyError when the key is absent and no default is given —
    a silent None here would surface much later as a confusing failure
    deep inside a cost calculation.
    """
    if key in _cache:
        return _cache[key]

    crud = _crud()
    raw = crud.get_raw(key)
    if raw is None:
        if default is _MISSING:
            raise KeyError(
                f"config key '{key}' not found. Add it to DEFAULT_CONFIG in "
                f"database/schema.py, or set it with config.set_value()."
            )
        return default

    value = crud.deserialise(raw[0], raw[1])
    _cache[key] = value
    return value


def get_int(key, default=_MISSING):
    return int(get(key, default))


def get_float(key, default=_MISSING):
    return float(get(key, default))


def get_bool(key, default=_MISSING):
    value = get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def get_str(key, default=_MISSING):
    return str(get(key, default))


def get_json(key, default=_MISSING):
    return get(key, default)


def get_category(category):
    """Every key in one category, as a {key: typed value} dict."""
    crud = _crud()
    return {
        k: crud.deserialise(v[0], v[1])
        for k, v in crud.get_all_raw(category).items()
    }


def all_values():
    """Every key in the system, as a {key: typed value} dict."""
    crud = _crud()
    return {
        k: crud.deserialise(v[0], v[1])
        for k, v in crud.get_all_raw().items()
    }


# ----------------------------------------------------------------------
# Writer
# ----------------------------------------------------------------------
def set_value(key, value, value_type=None, category=None, description=None,
              source="api"):
    """Update `key` and invalidate its cache entry.

    The change is recorded in `config_history`.
    """
    result = _crud().set_value(
        key, value, value_type=value_type, category=category,
        description=description, source=source,
    )
    invalidate(key)
    return result


def fingerprint(keys=None):
    """Short stable hash of the configuration, for lineage records."""
    return _crud().fingerprint(keys)


def feature_fingerprint():
    """Fingerprint of only the keys that change what a feature VALUE means.

    The feature store versions on this, so a change to (say) the lag list
    produces a new feature version, while a change to a cloud price does
    not invalidate features that never depended on it.
    """
    return _crud().fingerprint(get_json("features.defining_keys"))


# ----------------------------------------------------------------------
# Backwards compatibility
# ----------------------------------------------------------------------
# Modules written against the old constant-based config still say
# `from config import HEADROOM`. Rather than break them all at once,
# those names resolve through to the database on attribute access
# (PEP 562). New code should call get_float("policy.headroom") directly.
_LEGACY_KEYS = {
    "DISK_PATH": "collector.disk_path",
    "SAMPLE_INTERVAL": "collector.sample_interval_sec",
    "COST_PER_VCPU_HOUR": "cost.vcpu_hour",
    "COST_PER_GB_RAM_HOUR": "cost.gb_ram_hour",
    "COST_PER_GB_STORAGE_MONTH": "cost.gb_storage_month",
    "HOURS_PER_MONTH": "cost.hours_per_month",
    "NODE_VCPUS": "node.vcpus",
    "NODE_RAM_GB": "node.ram_gb",
    "NODE_STORAGE_GB": "node.storage_gb",
    "HEADROOM": "policy.headroom",
    "FORECAST_HORIZON": "model.forecast_horizon",
}


def __getattr__(name):
    if name in _LEGACY_KEYS:
        return get(_LEGACY_KEYS[name])
    raise AttributeError(f"module 'config' has no attribute '{name}'")

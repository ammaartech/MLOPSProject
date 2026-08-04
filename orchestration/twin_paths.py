"""
The wall between a twin run and production.

A twin run executes the real pipeline — the real ETL, the real trainer,
the real promotion gate, the real recommender — against constructed data.
Everything that makes that convincing also makes it dangerous: the code
does not know it is a rehearsal, so every artifact it writes lands
wherever the environment says artifacts go. Without this module a scenario
run trains three models and drops them into `data/models/` alongside the
ones actually serving production, registers them in MLflow, and moves the
stream watermark.

So the twin gets its own everything, derived from the database it was
handed:

    data/metrics_twin_regime_change.db          the twin database
    data/twin_artifacts/metrics_twin_regime_change/
        models/                                  its .joblib files
        mlflow.db                                its tracking store
        stream_watermark.json                    its own watermark

Import order matters and is the reason this module exists separately from
`config`. `config.DB_PATH`, `DATA_DIR`, `MODEL_DIR` and `MLFLOW_URI` are
resolved once, at module load, from the environment; `database.connection`
then binds `DB_PATH` by value. Anything that wants to redirect them has to
run before the first `import config` anywhere in the process — so this
module imports nothing from the project.
"""

import os

# The value inherited from whoever launched this process, captured before
# any twin code overwrites it. In Docker and k8s this is the production
# database of that environment, and it is the one path most worth
# refusing: `dataset/metrics.db` is not production everywhere, but
# whatever the parent was already pointed at always is.
_INHERITED_DB = os.environ.get("RESOURCE_MONITOR_DB")
_INHERITED_DB_DIR = os.environ.get("RESOURCE_MONITOR_DB_DIR")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def production_paths():
    """Every database path a twin run must never open, absolute."""
    candidates = [
        # The tracked evidence. `dataset/` is committed like source
        # precisely because no re-run can reproduce it.
        os.path.join(ROOT, "dataset", "metrics.db"),
        # What the k8s overlays and compose use.
        os.path.join(ROOT, "data", "metrics.db"),
    ]
    if _INHERITED_DB:
        candidates.append(_INHERITED_DB)
    if _INHERITED_DB_DIR:
        candidates.append(os.path.join(_INHERITED_DB_DIR, "metrics.db"))

    return {os.path.normcase(os.path.abspath(p)) for p in candidates}


def assert_not_production(db_path):
    """Refuse to proceed unless `db_path` is unmistakably a twin database.

    Two independent conditions, because either one alone has a hole. The
    name check catches a path that has not been created yet — which is the
    normal case, since the generator rebuilds the file — and the identity
    check catches a file called `metrics_twin.db` that has been symlinked
    or bind-mounted onto the real one.

    Raised as an exception rather than left to `assert`: `python -O` strips
    assert statements, and a guard that disappears under an optimisation
    flag is not a guard. `run_twin` keeps a plain assert as well, so the
    invariant is visible at the top of the file that depends on it.
    """
    resolved = os.path.normcase(os.path.abspath(db_path))
    name = os.path.basename(resolved).lower()

    if "twin" not in name:
        raise RuntimeError(
            f"Refusing to run: '{db_path}' is not a twin database.\n"
            f"  A twin database must have 'twin' in its filename, so that a "
            f"mistyped path cannot quietly replay synthetic load into the "
            f"collected evidence.\n"
            f"  Try --db data/metrics_twin.db"
        )

    forbidden = production_paths()
    if resolved in forbidden:
        raise RuntimeError(
            f"Refusing to run: '{db_path}' resolves to a production "
            f"database.\n  Production paths on this machine: "
            f"{', '.join(sorted(forbidden))}"
        )

    # `os.path.samefile` is the only check that sees through a symlink,
    # a junction or a bind mount, and it needs both files to exist.
    if os.path.exists(resolved):
        for candidate in forbidden:
            if os.path.exists(candidate):
                try:
                    if os.path.samefile(resolved, candidate):
                        raise RuntimeError(
                            f"Refusing to run: '{db_path}' is the same file "
                            f"as the production database '{candidate}'."
                        )
                except OSError:
                    # Unreadable or on a filesystem that cannot answer.
                    # The two checks above already stand on their own.
                    continue

    return resolved


def artifact_root(db_path):
    """The directory this twin owns, derived from its database filename."""
    resolved = os.path.abspath(db_path)
    stem = os.path.splitext(os.path.basename(resolved))[0]
    return os.path.join(os.path.dirname(resolved), "twin_artifacts", stem)


def isolate_artifacts(db_path):
    """Redirect models, MLflow and the watermark away from production.

    Must be called BEFORE the first `import config` in the process. All
    three variables are set explicitly rather than relying on
    `RESOURCE_MONITOR_DATA` alone, because compose and the k8s overlays
    set `RESOURCE_MONITOR_MODELS` directly and an inherited value would
    otherwise win.
    """
    root = artifact_root(db_path)
    models = os.path.join(root, "models")

    os.makedirs(models, exist_ok=True)

    os.environ["RESOURCE_MONITOR_DATA"] = root
    os.environ["RESOURCE_MONITOR_MODELS"] = models
    os.environ["RESOURCE_MONITOR_MLFLOW"] = "sqlite:///" + os.path.join(
        root, "mlflow.db")

    return root

#!/usr/bin/env bash
# ======================================================================
#  Container entrypoint — the Linux counterpart of run.bat.
#
#  Same verbs, same module-invocation rule (-m, dotted, no .py). The
#  prerequisite steps run.bat performs on Windows (interpreter, venv, pip)
#  are already satisfied by the image, so only the database step remains.
#
#      docker compose run --rm app pipeline
#      docker compose run --rm app drift
#      docker compose up dashboard
# ======================================================================
set -euo pipefail

ACTION="${1:-dashboard}"
shift || true

DATA_DIR="${RESOURCE_MONITOR_DATA:-/app/data}"
# metrics.db lives apart from the regenerable output: compose mounts the
# repository's tracked ./dataset here, k8s points it at the namespace's
# own volume instead. See config.py.
DB_DIR="${RESOURCE_MONITOR_DB_DIR:-/app/dataset}"

# ----------------------------------------------------------------------
#  Database: create tables, seed defaults, apply column migrations.
#  Idempotent, and cheap enough to run on every container start.
#
#  Note it does NOT rewrite `collector.disk_path`. A database created on
#  Windows stores "C:\" there, which does not exist in this container —
#  but the same file is shared with the host, so correcting it here would
#  break the host. collector.psutil_logger.resolve_disk_path() substitutes
#  the platform root at read time instead.
# ----------------------------------------------------------------------
init_database() {
    mkdir -p "$DATA_DIR" "$DATA_DIR/models" "$DB_DIR"
    python - <<'PY'
import sys

import config
from database.connection import get_connection

connection = get_connection()
if connection is None:
    sys.exit("database initialisation failed")
connection.close()

print(f"   database ready at {config.DB_PATH}")
PY
}

echo ""
echo "[entrypoint] action: ${ACTION}"
echo "[entrypoint] data  : ${DATA_DIR}"
echo "[entrypoint] db    : ${DB_DIR}"
echo "[entrypoint] initialising database..."
init_database
echo ""

case "$ACTION" in
    setup)
        echo "Prerequisites satisfied."
        ;;

    collect)
        # NOTE: psutil inside a Linux container reports the container's
        # view of its cgroup and the Docker Desktop VM — NOT the Windows
        # host. Use this to demonstrate the collector; collect the data
        # you actually model on the host with `run.bat collect`.
        echo "Collecting metrics — Ctrl+C to stop."
        exec python -m collector.psutil_logger "$@"
        ;;

    load)
        MINUTES="${1:-15}"
        echo "Running the load generator for ${MINUTES} minute(s)..."
        exec python -m collector.load_generator --minutes "$MINUTES"
        ;;

    pipeline)
        exec python -m orchestration.run_pipeline "$@"
        ;;

    drift)
        exec python -m serving.drift "$@"
        ;;

    schedule)
        echo "Continuous loop — Ctrl+C to stop."
        exec python -m orchestration.scheduler "$@"
        ;;

    dashboard)
        echo "Dashboard on http://localhost:8501"
        exec streamlit run dashboard/app.py "$@"
        ;;

    menu)
        exec python main.py "$@"
        ;;

    mlflow)
        echo "MLflow UI on http://localhost:5000"
        # Four slashes: sqlite:/// is the scheme, the fourth begins the
        # absolute path. Three would make it relative to the working
        # directory and silently create a second, empty database.
        exec mlflow ui \
            --backend-store-uri "sqlite:////${DATA_DIR#/}/mlflow.db" \
            --host 0.0.0.0 --port 5000
        ;;

    shell)
        exec /bin/bash "$@"
        ;;

    *)
        # Anything unrecognised runs verbatim, so `docker compose run app
        # python -m evaluation.backtest` works without a case for it.
        exec "$ACTION" "$@"
        ;;
esac

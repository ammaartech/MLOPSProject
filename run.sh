#!/usr/bin/env bash
# ======================================================================
#  Predictive Resource Monitoring System — Mac/Linux launcher
# ======================================================================

set -e
cd "$(dirname "$0")"

VENV_DIR=".venv"
VENV_PY="$VENV_DIR/bin/python"
DEPS_STAMP="$VENV_DIR/.deps_ok"

ACTION="${1:-menu-interactive}"
ARG2="$2"

if [ "$ACTION" = "reset-deps" ]; then
    ACTION="setup"
fi

# ================================================================
#  DOCKER ACTIONS
# ================================================================
if [[ "$ACTION" == docker-* ]] || [[ "$ACTION" == k8s-* ]]; then
    if ! command -v docker &> /dev/null; then
        echo "ERROR: cannot reach the Docker daemon. Start Docker Desktop."
        exit 1
    fi
    case "$ACTION" in
        docker-build) docker compose build ;;
        docker-up) docker compose up -d dashboard mlflow; echo -e "\ndashboard: http://localhost:8501\nMLflow: http://localhost:5000" ;;
        docker-down) docker compose down ;;
        docker-pipeline) docker compose run --rm app pipeline ;;
        docker-drift) docker compose run --rm app drift ;;
        *) echo "Action $ACTION is better run manually via docker compose or kubectl on Mac/Linux." ;;
    esac
    exit 0
fi

# ================================================================
#  PREREQUISITES (Python, Venv, Deps, Database)
# ================================================================
echo "[prereq] checking Python..."
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 not found. Please install Python 3.11+"
    exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "[prereq] creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    rm -f "$DEPS_STAMP"
fi

if [ ! -f "$DEPS_STAMP" ]; then
    echo "[prereq] installing dependencies..."
    "$VENV_PY" -m pip install --upgrade pip --quiet
    "$VENV_PY" -m pip install -r requirements.txt
    touch "$DEPS_STAMP"
fi

echo "[prereq] initialising database..."
"$VENV_PY" -c "from database.connection import get_connection; c = get_connection(); c.close()"

echo "[prereq] all prerequisites satisfied."
if [ "$ACTION" = "setup" ]; then exit 0; fi

# ================================================================
#  INTERACTIVE MENU
# ================================================================
if [ "$ACTION" = "menu-interactive" ]; then
    echo "======================================================================"
    echo "  PREDICTIVE RESOURCE MONITORING SYSTEM (Mac/Linux)"
    echo "======================================================================"
    echo "  1  Data entry & logging (Collect metrics + Pipeline cycle)"
    echo "  2  Dashboard (Streamlit)"
    echo ""
    echo "  0  Exit"
    echo ""
    read -p "Select: " CHOICE
    case "$CHOICE" in
        1) ACTION="schedule" ;;
        2) ACTION="dashboard" ;;
        0) exit 0 ;;
        *) echo "Invalid selection"; exit 1 ;;
    esac
fi

# ================================================================
#  EXECUTE ACTION
# ================================================================
case "$ACTION" in
    collect) "$VENV_PY" -m collector.psutil_logger ;;
    load) "$VENV_PY" -m collector.load_generator --minutes "${ARG2:-15}" ;;
    pipeline) "$VENV_PY" -m orchestration.run_pipeline ;;
    drift) "$VENV_PY" -m serving.drift ;;
    schedule) "$VENV_PY" -m orchestration.scheduler --with-collector ;;
    dashboard) "$VENV_DIR/bin/streamlit" run dashboard/app.py ;;
    menu) "$VENV_PY" main.py ;;
    mlflow) "$VENV_DIR/bin/mlflow" ui --backend-store-uri sqlite:///data/mlflow.db ;;
    *) echo "Unknown action: $ACTION" ;;
esac

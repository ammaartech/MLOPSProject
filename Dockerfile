# syntax=docker/dockerfile:1
# ======================================================================
#  Predictive Resource Monitoring System
#
#  Two stages. The builder installs the pinned dependency set into a
#  self-contained virtualenv; the runtime copies only that virtualenv and
#  the source. Nothing from pip's build machinery reaches the final image.
#
#  Python 3.12 matches the .venv this system was measured against
#  (3.12.1), so the resolved wheel set is the same one the results in
#  README.md came from.
# ======================================================================

# ---------------------------------------------------------------- builder
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential is a safety net: every pin in requirements.txt has a
# cp312 manylinux wheel today, but a source fallback failing at build
# time is a far worse failure mode than a slightly slower builder stage.
# It stays in the builder and never reaches the runtime image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied alone so the dependency layer is cached independently of source
# changes. Editing forecast.py must not trigger a scikit-learn rebuild.
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install -r requirements.txt


# ---------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Predictive Resource Monitoring System" \
      org.opencontainers.image.description="Forecasts CPU/memory/storage, recommends allocation, prices it, and replays the result to verify the saving." \
      org.opencontainers.image.source="https://github.com/ammaartech/MLOPSProject" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Every entry point is a module (-m, dotted). Without this, an import
    # of `config` from inside dashboard/ or collector/ fails.
    PYTHONPATH=/app \
    # The database, models and MLflow store all live under one directory
    # so a single volume persists the whole system's state.
    RESOURCE_MONITOR_DATA=/app/data \
    # matplotlib defaults to a GUI backend and to $HOME for its cache;
    # neither exists here.
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    # MLflow tries to stamp each run with the source git SHA. There is no
    # repository in the image and no reason to add one, and without this
    # every single run prints forty lines of GitPython refresh warnings.
    GIT_PYTHON_REFRESH=quiet \
    # Streamlit must bind every interface to be reachable from the host,
    # and must not try to open a browser or phone home.
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# procps gives the container a working `ps`, which psutil does not need
# but anyone debugging the collector inside the container will.
RUN apt-get update \
 && apt-get install -y --no-install-recommends procps tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

COPY . .

# Created before the volume is attached so the directory exists and is
# owned correctly even when nothing is mounted over it. /app/dataset is
# where config.py expects metrics.db; compose mounts the host's tracked
# copy over it.
RUN mkdir -p /app/data /app/data/models /app/dataset /app/mlruns

EXPOSE 8501 5000

# The scheduler and collector run until interrupted. Without an init
# process, PID 1 is Python, which ignores SIGTERM by default — `docker
# stop` would then wait the full 10s timeout and SIGKILL every time.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["dashboard"]

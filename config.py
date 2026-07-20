import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "metrics.db")

# Cross-platform disk root: "C:\" on Windows, "/" on Linux/Mac
DISK_PATH = os.path.abspath(os.sep)

# Seconds between samples
SAMPLE_INTERVAL = 5


# ==========================================================
# CLOUD COST MODEL — AWS us-east-1, Linux on-demand
# Rates verified July 2026. Sources noted per line.
# Using a Fargate-style per-resource split so CPU / memory /
# storage are priced independently (matches our 3 forecasts).
# ==========================================================

# Compute: AWS Fargate on-demand, us-east-1
COST_PER_VCPU_HOUR = 0.04048     # $/vCPU-hour
COST_PER_GB_RAM_HOUR = 0.004445  # $/GB-RAM-hour

# Storage: EBS gp3, us-east-1 (~$0.08/GB-month)
COST_PER_GB_STORAGE_MONTH = 0.08  # $/GB-month

# --- Capacity of one reference node (what % maps to) ---
# We model a node with these totals; utilization % is taken
# against these to convert "70% CPU" -> actual vCPU/GB numbers.
NODE_VCPUS = 8          # total vCPUs on the reference node
NODE_RAM_GB = 32        # total RAM on the reference node
NODE_STORAGE_GB = 500   # total provisioned storage on the node

# --- Recommender policy knobs ---
HEADROOM = 0.20         # 20% safety buffer above forecasted peak
FORECAST_HORIZON = 20   # how many steps ahead to forecast (20 x 3s = 60s)
HOURS_PER_MONTH = 730   # standard cloud billing month
import time
import psutil
from datetime import datetime

from config import DISK_PATH, SAMPLE_INTERVAL
from crud.metrics_crud import create_metric, count_metrics


def collect_metrics():
    # interval=1 blocks ~1s and returns CPU % over that second (accurate)
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(DISK_PATH)

    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "cpu_percent": cpu,
        "mem_percent": mem.percent,
        "mem_used_mb": round(mem.used / (1024 ** 2), 2),
        "disk_percent": disk.percent,
        "disk_used_gb": round(disk.used / (1024 ** 3), 2),
    }


def log_once(verbose=True):
    record = collect_metrics()
    create_metric(record)
    if verbose:
        print(
            f"[{record['ts']}]  "
            f"CPU {record['cpu_percent']:5.1f}%  |  "
            f"MEM {record['mem_percent']:5.1f}%  |  "
            f"DISK {record['disk_percent']:5.1f}%"
        )
    return record


def run_logger(interval=SAMPLE_INTERVAL, duration=None):
    print(f"Logger started | interval={interval}s | duration={duration or 'infinite'}")
    start = time.time()
    try:
        while True:
            log_once()
            if duration and (time.time() - start) >= duration:
                break
            # cpu_percent already blocked ~1s, so sleep the remainder
            time.sleep(max(0, interval - 1))
    except KeyboardInterrupt:
        print("\nLogger stopped by user.")
    print("Total records in DB:", count_metrics())


if __name__ == "__main__":
    # quick smoke test: log for 60s at 5s cadence
    run_logger(interval=5, duration=60)
import os
import time
import psutil
from datetime import datetime

import config
from crud.metrics_crud import create_metric, count_metrics

# Warned about once per process rather than once per sample; the collector
# takes a reading every 3 seconds.
_disk_path_warned = False


def resolve_disk_path():
    """The configured disk path, or this platform's root if it is absent.

    `collector.disk_path` is seeded from os.path.abspath(os.sep), so a
    database created on Windows stores "C:\\". The same database opened
    inside a Linux container names a path that does not exist there, and
    psutil.disk_usage raises on it.

    The config table still owns the value — this only substitutes when the
    stored path is missing on the machine actually taking the reading, and
    it deliberately does NOT write the substitute back. The database is
    shared with the Windows host, and correcting it for one platform would
    break it for the other.
    """
    global _disk_path_warned

    configured = config.get_str("collector.disk_path")
    if os.path.exists(configured):
        return configured

    fallback = os.path.abspath(os.sep)
    if not _disk_path_warned:
        print(f"  [collector] configured disk path '{configured}' does not "
              f"exist on this host; reading '{fallback}' instead")
        _disk_path_warned = True
    return fallback


def collect_metrics():
    # interval=1 blocks ~1s and returns CPU % over that second (accurate)
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(resolve_disk_path())

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


def run_logger(interval=None, duration=None, verbose=True):
    # Read at call time, not as a default argument: a default is bound
    # once at import, so the collector would keep using whatever the
    # interval was when the module first loaded even after it changed.
    interval = interval or config.get_int("collector.sample_interval_sec")

    print(f"Logger started | interval={interval}s | duration={duration or 'infinite'}")
    start = time.time()
    try:
        while True:
            log_once(verbose=verbose)
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
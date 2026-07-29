import time
import psutil
from datetime import datetime, timezone

import config
from crud.metrics_crud import create_metric, count_metrics


def collect_metrics():
    """Sample CPU, memory, and instantaneous disk I/O throughput.

    Disk I/O is measured by snapshotting ``psutil.disk_io_counters()``
    before and after the 1-second ``cpu_percent(interval=1)`` blocking
    call, so read/write MB/s come for free without adding latency.
    """
    # Snapshot disk I/O counters BEFORE the 1-second CPU measurement
    io_before = psutil.disk_io_counters()

    # interval=1 blocks ~1s and returns CPU % over that second (accurate)
    cpu = psutil.cpu_percent(interval=1)

    # Snapshot disk I/O counters AFTER — the delta is the 1-second throughput
    io_after = psutil.disk_io_counters()

    mem = psutil.virtual_memory()

    read_bytes = io_after.read_bytes - io_before.read_bytes
    write_bytes = io_after.write_bytes - io_before.write_bytes

    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cpu_percent": cpu,
        "mem_percent": mem.percent,
        "mem_used_mb": round(mem.used / (1024 ** 2), 2),
        "disk_read_mb_s": round(read_bytes / (1024 ** 2), 2),
        "disk_write_mb_s": round(write_bytes / (1024 ** 2), 2),
    }


def log_once(verbose=True):
    record = collect_metrics()
    create_metric(record)
    if verbose:
        print(
            f"[{record['ts']}]  "
            f"CPU {record['cpu_percent']:5.1f}%  |  "
            f"MEM {record['mem_percent']:5.1f}%  |  "
            f"DISK R {record['disk_read_mb_s']:7.2f} MB/s  "
            f"W {record['disk_write_mb_s']:7.2f} MB/s"
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
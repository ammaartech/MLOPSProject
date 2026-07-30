"""
The continuous loop — what "continuously analyses" means in practice.

    python -m orchestration.scheduler --with-collector

Each cycle:

    1. incremental ETL      only rows newer than the stream watermark
    2. refresh online store the current feature vector per resource
    3. predict              the champion, logged for later scoring
    4. score                backfill actuals whose moment has passed
    5. drift check          every `--drift-every` cycles
    6. recommend            allocation and cost, written to the DB
    7. retention            purge raw rows past the retention window

Why the ETL is incremental
--------------------------
A collector appends forever. Reprocessing the whole table each cycle gets
slower without bound, so `stream://` reads from a watermark and only sees
what arrived since. The full-history path stays available for training,
where it belongs.

What the scheduler does NOT do
------------------------------
It never promotes a model. Drift can trigger a retrain, and the retrain
faces the same gate as any other challenger. Nothing reaches production
without clearing the baseline, whatever the schedule decides.
"""

import argparse
import signal
import sys
import threading
import time
from datetime import datetime, timedelta

import config

_stop = threading.Event()


def _handle_signal(_signum, _frame):
    print("\n  stopping after the current cycle...")
    _stop.set()


# ----------------------------------------------------------------------
# Background collector
# ----------------------------------------------------------------------
def start_collector(interval=None, quiet=False):
    """Run the psutil logger on a daemon thread."""
    from collector.psutil_logger import log_once

    interval = interval or config.get_int("collector.sample_interval_sec")

    def loop():
        while not _stop.is_set():
            try:
                log_once(verbose=not quiet)
            except Exception as exc:                       # noqa: BLE001
                print(f"  [collector] {type(exc).__name__}: {exc}")
            # cpu_percent already blocked ~1s inside log_once.
            _stop.wait(max(0.0, interval - 1))

    thread = threading.Thread(target=loop, name="collector", daemon=True)
    thread.start()
    return thread


# ----------------------------------------------------------------------
# One cycle
# ----------------------------------------------------------------------
def cycle(number, drift_every=10, verbose=True):
    from model.features import load_clean_frame
    from model.feature_store import refresh_online
    from pipeline import etl
    from serving.predictor import predict_all, score

    stamp = datetime.now().strftime("%H:%M:%S")
    report = {"cycle": number, "at": stamp}

    # --- 1. incremental ETL --------------------------------------------
    # Read the full history for cleaning (gap segmentation needs context),
    # but note how many rows are genuinely new for the log.
    from pipeline.sources import StreamSource

    new_rows = len(StreamSource(advance=True).read())
    report["new_rows"] = new_rows

    result = etl.run("sqlite://", verbose=False)
    report["etl_status"] = result["status"]
    if result["status"] != "success":
        report["error"] = result.get("blocked_reason", result["status"])
        if verbose:
            print(f"  [{stamp}] cycle {number}: ETL {result['status']} — "
                  f"{report.get('error')}")
        return report

    run_id = result["run_id"]
    frame = load_clean_frame(run_id)
    report["rows"] = len(frame)

    # --- 2. refresh the online feature store ----------------------------
    refresh_online(frame, data_fingerprint=result["data_fingerprint"])

    # --- 3-4. serve and score -------------------------------------------
    predictions = predict_all(frame)
    report["predictions"] = {
        target: (round(value, 3) if value is not None else None)
        for target, (value, _meta) in predictions.items()
    }
    report["scored"] = score(frame)["scored"]

    # --- 4b. process alerting -------------------------------------------
    try:
        from service import process_alert
        process_alert.check()
    except Exception as exc:
        print(f"  [process_alert] Check failed: {exc}")

    # --- 5. drift --------------------------------------------------------
    if number % drift_every == 0:
        from serving.drift import monitor

        decisions = monitor(frame)
        drifted = [t for t, d in decisions.items()
                   if t != "_retrain" and d.get("drifted")]
        report["drift"] = drifted or "none"
        if drifted and verbose:
            print(f"  [{stamp}] DRIFT on {', '.join(drifted)} — "
                  f"retrain triggered, gate will decide")

    # --- 6. recommend ----------------------------------------------------
    from service.recommender import build_recommendation

    recommendation = build_recommendation(frame, persist=True)
    report["monthly_cost"] = recommendation["predictive_cost"]["total"]
    report["savings_pct"] = recommendation["savings_percent"]

    # --- 7. retention -----------------------------------------------------
    if config.get_bool("retention.enabled"):
        report["purged"] = apply_retention()

    if verbose:
        values = "  ".join(
            f"{t.split('_')[0]}={v}" for t, v in report["predictions"].items()
            if v is not None
        )
        print(f"  [{stamp}] cycle {number:>4}  +{new_rows:>3} rows  "
              f"{values}  ${report['monthly_cost']}/mo "
              f"({report['savings_pct']}%)  scored={report['scored']}")

    return report


def apply_retention():
    """Purge raw rows older than the retention window."""
    from crud.metrics_crud import purge_before

    days = config.get_int("retention.raw_days")
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    return purge_before(cutoff) or 0


# ----------------------------------------------------------------------
def run(interval=None, cycles=None, drift_every=10, with_collector=False,
        verbose=True):
    interval = interval or config.get_int("collector.sample_interval_sec") * 10

    signal.signal(signal.SIGINT, _handle_signal)

    print("=" * 78)
    print("SCHEDULER")
    print("=" * 78)
    print(f"  cycle interval : {interval}s")
    print(f"  drift check    : every {drift_every} cycles")
    print(f"  collector      : {'in-process' if with_collector else 'external'}")
    print(f"  cycles         : {cycles or 'until interrupted (Ctrl+C)'}")
    print("=" * 78)

    if with_collector:
        start_collector()
        print("  collector thread started; waiting for the first samples...")
        _stop.wait(min(interval, 10))

    number, reports = 0, []
    while not _stop.is_set():
        number += 1
        try:
            reports.append(cycle(number, drift_every=drift_every, verbose=verbose))
        except Exception as exc:                           # noqa: BLE001
            print(f"  cycle {number} failed: {type(exc).__name__}: {exc}")

        if cycles and number >= cycles:
            break
        _stop.wait(interval)

    _stop.set()
    print(f"\n  stopped after {number} cycle(s)")
    return reports


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the continuous loop.")
    parser.add_argument("--interval", type=int, default=None,
                        help="seconds between cycles")
    parser.add_argument("--cycles", type=int, default=None,
                        help="stop after N cycles (default: run until Ctrl+C)")
    parser.add_argument("--drift-every", type=int, default=10)
    parser.add_argument("--with-collector", action="store_true",
                        help="run the psutil collector in this process too")
    parser.add_argument("--with-twin", type=str, default=None,
                        help="run a digital twin simulation for the named scenario and exit")
    args = parser.parse_args()

    if args.with_twin:
        import subprocess
        import sys
        cmd = [sys.executable, "-m", "orchestration.run_twin", "--scenario", args.with_twin]
        print(f"Starting digital twin run for scenario '{args.with_twin}'...")
        res = subprocess.run(cmd)
        sys.exit(res.returncode)

    run(interval=args.interval, cycles=args.cycles,
        drift_every=args.drift_every, with_collector=args.with_collector)
    sys.exit(0)

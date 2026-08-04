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
import atexit
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta

import config

_stop = threading.Event()


# ----------------------------------------------------------------------
# Single-instance lock
# ----------------------------------------------------------------------
# `run.bat` starts the scheduler in its own window and the dashboard in
# another, so running run.bat twice would leave two schedulers sampling
# the same machine into the same table. That is not merely wasteful: two
# collectors at a 3-second cadence write near-identical timestamps, which
# the validation stage correctly reports as duplicates, and the cadence
# check then measures an interval that never actually happened.
#
# A PID file rather than an OS file lock: the failure this guards against
# is a second launch minutes later, not a race, and a PID file can say
# WHICH process holds it in the error message.
LOCK_PATH = os.path.join(config.DATA_DIR, "scheduler.lock")


def _holder():
    """The PID of a live scheduler holding the lock, or None.

    A stale file — left by a process that was killed rather than stopped —
    is treated as absent. PIDs are recycled, so the name is checked too;
    without that, a fresh process that happened to inherit the number
    would lock the scheduler out permanently.
    """
    try:
        with open(LOCK_PATH) as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        return None

    if pid == os.getpid():
        return None

    try:
        import psutil

        process = psutil.Process(pid)
        if "python" not in process.name().lower():
            return None
        return pid
    except Exception:                                          # noqa: BLE001
        return None


def acquire_lock():
    """Claim the lock. Returns (True, pid) or (False, holding_pid)."""
    holding = _holder()
    if holding is not None:
        return False, holding

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    with open(LOCK_PATH, "w") as handle:
        handle.write(str(os.getpid()))
    atexit.register(release_lock)
    return True, os.getpid()


def release_lock():
    """Drop the lock, but only if this process is the one holding it."""
    try:
        with open(LOCK_PATH) as handle:
            if int(handle.read().strip()) != os.getpid():
                return
        os.remove(LOCK_PATH)
    except (OSError, ValueError):
        pass


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

    # --- 4b. process alert -----------------------------------------------
    # Positioned after predict because it reads mem_percent from the online
    # feature store that step 2 refreshed, and BEFORE drift and recommend
    # because it must not influence either. It is an observation about who
    # is using memory, not an input to what gets allocated — so it is
    # wrapped, its own module swallows its own failures, and nothing below
    # reads its result.
    from service import process_alert

    alert = process_alert.check(verbose=verbose)
    report["process_alert"] = bool(alert)

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
def run(interval=None, cycles=None, drift_every=None, with_collector=False,
        verbose=True, single_instance=True):
    # Both read from config rather than being derived in code. The
    # interval used to be `collector.sample_interval_sec * 10`, which tied
    # how often the system re-forecasts to how often it samples — two
    # unrelated decisions — and buried the multiplier where no operator
    # would look for it.
    interval = interval or config.get_int("scheduler.interval_sec")
    drift_every = drift_every or config.get_int("scheduler.drift_every")

    if single_instance:
        claimed, holder = acquire_lock()
        if not claimed:
            print(f"  A scheduler is already running (PID {holder}).")
            print(f"  Two of them would sample this machine twice into the "
                  f"same table, which reads downstream as duplicate "
                  f"timestamps and a cadence that never happened.")
            print(f"  Stop that one first, or delete {LOCK_PATH} if it is "
                  f"stale.")
            return []

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
        started_at = time.monotonic()
        try:
            reports.append(cycle(number, drift_every=drift_every, verbose=verbose))
        except Exception as exc:                           # noqa: BLE001
            print(f"  cycle {number} failed: {type(exc).__name__}: {exc}")

        if cycles and number >= cycles:
            break

        # Sleep the REMAINDER of the interval, not the whole of it.
        #
        # Waiting a full `interval` after each cycle makes the period
        # `interval + however long the cycle took`, so a 15s setting with
        # a 3s cycle actually fired every 18s — the configured number was
        # a gap, not a cadence, which is not what "run every 15 seconds"
        # means to anyone reading it.
        #
        # `time.monotonic` rather than wall clock: this must not lurch
        # when the system clock is adjusted or the machine resumes from
        # sleep. When a cycle overruns the interval the remainder is
        # negative, the wait is skipped, and the next cycle starts
        # immediately — late, but never concurrent, because this loop is
        # single-threaded and nothing here can re-enter `cycle`.
        remaining = interval - (time.monotonic() - started_at)
        if remaining > 0:
            _stop.wait(remaining)
        elif verbose:
            print(f"  cycle {number} took longer than the {interval}s "
                  f"interval; starting the next one immediately")

    _stop.set()
    release_lock()
    print(f"\n  stopped after {number} cycle(s)")
    return reports


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the continuous loop.")
    parser.add_argument("--interval", type=int, default=None,
                        help="seconds between cycles")
    parser.add_argument("--cycles", type=int, default=None,
                        help="stop after N cycles (default: run until Ctrl+C)")
    parser.add_argument("--drift-every", type=int, default=None,
                        help="drift check every N cycles "
                             "(default: config scheduler.drift_every)")
    parser.add_argument("--with-collector", action="store_true",
                        help="run the psutil collector in this process too")
    parser.add_argument("--allow-multiple", action="store_true",
                        help="skip the single-instance lock. Only correct "
                             "when each instance points at a different "
                             "database via RESOURCE_MONITOR_DB.")
    from collector.scenario_generator import SCENARIOS

    parser.add_argument("--with-twin", type=str, default=None,
                        choices=SCENARIOS + ("all",),
                        help="generate and replay the named synthetic "
                             "scenario against an isolated database, then "
                             "exit. Does not run the live loop.")
    args = parser.parse_args()

    # A distinct code path, deliberately not a branch inside cycle().
    #
    # The live loop and the twin have opposite requirements: the loop reads
    # the production database forever, the twin binds a different database
    # for one run and must do so before `config` is imported. Sharing a
    # process would mean one of the two silently writing to the other's
    # database — so the twin gets its own interpreter and this function
    # never reaches `run()`.
    if args.with_twin:
        import subprocess

        twin = ["-m", "orchestration.run_twin"]
        twin += ["--all"] if args.with_twin == "all" else [
            "--scenario", args.with_twin, "--generate"]

        print(f"Twin run: {args.with_twin} (the live loop will NOT start)")
        sys.exit(subprocess.run([sys.executable] + twin).returncode)

    run(interval=args.interval, cycles=args.cycles,
        drift_every=args.drift_every, with_collector=args.with_collector,
        single_instance=not args.allow_multiple)
    sys.exit(0)

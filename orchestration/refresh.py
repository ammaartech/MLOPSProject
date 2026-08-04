"""
One pipeline pass, on demand — the replacement for the background loop.

    python -m orchestration.refresh

Does exactly what one scheduler cycle does, once, and exits:

    incremental ETL -> refresh the online feature store -> predict
    -> score -> process alert -> recommend and price

Why this exists instead of the scheduler
----------------------------------------
`orchestration/scheduler.py` runs the same work forever on a timer. That
turned out to be the wrong shape for a single laptop that is also serving
the dashboard from the same SQLite file:

* Every cycle re-ran the full ETL over the whole table. That cost grows
  with the collection — it is not a fixed price — so the loop got slower
  the longer it ran, and eventually cycles took longer than the interval
  between them.
* The loop, the in-process collector and Streamlit competed for the same
  database and the same CPU. The dashboard was measuring a machine that
  was busy largely because the dashboard's own data pipeline was running.
* Nothing was waiting on the result. A forecast recomputed every thirty
  seconds is only useful if something reads it every thirty seconds, and
  the only reader is a browser tab that a person is looking at or is not.

So the loop is gone and the work is explicit. The collector still runs
continuously — appending a row every few seconds is cheap, and it is what
makes the data live. Turning those rows into a forecast happens when
somebody asks, from the dashboard's "Refresh data" button or from this
command.

The scheduler module is still there and still works for a fixed number of
cycles (`--cycles N`), which is what the twin and any timed demonstration
want. It simply is not started automatically any more.
"""

import sys
import time


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Run one pipeline pass and exit.")
    parser.add_argument("--quiet", action="store_true",
                        help="summary only")
    args = parser.parse_args(argv)

    verbose = not args.quiet
    started = time.perf_counter()

    # `cycle` is imported rather than reimplemented: this must do exactly
    # what the loop did, or "refresh" and "a scheduler cycle" would drift
    # into meaning different things.
    #
    # drift_every is left at its default and the cycle number is 1, so
    # `1 % 10` is non-zero and the drift check is skipped. Drift needs a
    # run of scored predictions to say anything, and one pass cannot
    # produce one — it is a `python -m serving.drift` job, not part of a
    # refresh.
    from orchestration.scheduler import cycle

    print("Refreshing — ETL, features, forecast, recommendation...")
    try:
        report = cycle(1, verbose=verbose)
    except Exception as exc:                                   # noqa: BLE001
        print(f"  refresh failed: {type(exc).__name__}: {exc}")
        return 1

    elapsed = time.perf_counter() - started

    if report.get("etl_status") != "success":
        print(f"\n  ETL did not complete: "
              f"{report.get('error', report.get('etl_status'))}")
        print(f"  elapsed {elapsed:.1f}s")
        return 2

    print(f"\n  rows          : {report.get('rows')} cleaned "
          f"(+{report.get('new_rows', 0)} new since the last pass)")
    print(f"  predictions   : {report.get('predictions')}")
    print(f"  scored        : {report.get('scored')}")
    print(f"  monthly cost  : ${report.get('monthly_cost')} "
          f"({report.get('savings_pct')}% saved)")
    print(f"  elapsed       : {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

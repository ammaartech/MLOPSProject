"""
The CRUD operations on `metrics`, as a console surface.

One implementation, two callers: `main.py`'s Data / CRUD menu and
`run.bat`'s option 1 submenu, which invokes one operation per selection:

    python -m crud.console            the interactive loop
    python -m crud.console latest     a single operation, then exit

The prompting lives here rather than in either caller on purpose. batch
cannot parse a float, reject a bad record id or recover from a stray
keypress, so a submenu that asked for those itself would either duplicate
this logic badly or crash out of the menu on the first typo. run.bat asks
only which operation; everything after that happens in Python.

Every operation goes through `crud.metrics_crud`, so the whitelist on
`update_metric` and the parameterised SQL in `crud.query` apply here too.
"""

import sys
from datetime import datetime

from crud.metrics_crud import (
    count_metrics, create_metric, delete_metric, purge_before, read_all,
    read_between, read_latest, update_metric,
)
from crud.metrics_crud import _EDITABLE as EDITABLE_FIELDS


class _Abort(Exception):
    """Raised when the operator backs out of a prompt."""


# ----------------------------------------------------------------------
# Prompting
#
# Every prompt accepts a blank line as "cancel". Without that, the only
# exit from a half-finished record is Ctrl+C, which in the batch submenu
# also kills the loop that would have brought the menu back.
# ----------------------------------------------------------------------
def _ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else " (blank to cancel)"
    try:
        raw = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise _Abort
    if not raw:
        if default is not None:
            return str(default)
        raise _Abort
    return raw


def _ask_number(prompt, cast, default=None):
    while True:
        raw = _ask(prompt, default)
        try:
            return cast(raw)
        except ValueError:
            print(f"  Not a valid {cast.__name__}. Try again, or blank to cancel.")


def _ask_int(prompt, default=None):
    return _ask_number(prompt, int, default)


def _ask_float(prompt, default=None):
    return _ask_number(prompt, float, default)


def _ask_timestamp(prompt, default=None):
    """An ISO-8601 instant, validated before it can reach the table.

    `ts` is the column the whole system is ordered by: cleaning segments on
    the gaps between timestamps, every lag and rolling window is computed
    inside a segment, and the rollups resample on it. A value that is not a
    date does not fail loudly here — it is accepted as TEXT, sorts after
    every real timestamp because it compares as a string, and then shows up
    much later as an unparseable row in the ETL.
    """
    while True:
        raw = _ask(prompt, default)
        candidate = raw[:-1] if raw.endswith("Z") else raw
        try:
            datetime.fromisoformat(candidate)
            return raw
        except ValueError:
            print("  Not a timestamp. Use 2026-07-31T09:00:00 (or blank to "
                  "take the default).")


# ----------------------------------------------------------------------
# Display
# ----------------------------------------------------------------------
_HEADER = (f"\n{'ID':<6}{'Timestamp':<24}{'CPU%':>8}{'MEM%':>8}"
           f"{'MEM MB':>10}{'RD MB/s':>10}{'WR MB/s':>10}")


def _show(rows):
    """Rows are whole `metrics` records: id, ts, cpu, mem, mem_mb, rd, wr.

    The disk columns are megabytes per second, not percentages — an
    earlier version of this table printed `disk_read_mb_s` under a
    'DISK%' heading, which makes 0.08 MB/s read as 0.08% of a disk.
    """
    if not rows:
        print("  No records found.")
        return
    print(_HEADER)
    print("  " + "-" * 74)
    for r in rows:
        print(f"{r[0]:<6}{r[1]:<24}{r[2]:>8.1f}{r[3]:>8.1f}"
              f"{r[4]:>10.1f}{r[5]:>10.2f}{r[6]:>10.2f}")
    print(f"\n  {len(rows)} record(s).")


# ----------------------------------------------------------------------
# Operations
# ----------------------------------------------------------------------
def view_all():
    total = count_metrics()
    if total > 500:
        print(f"  {total} records. Printing all of them will scroll a long way.")
        if _ask("Continue? (y/n)", "n").lower() not in ("y", "yes"):
            return
    _show(read_all())


def view_latest():
    _show(read_latest(_ask_int("How many", 10)))


def view_between():
    start = _ask_timestamp("From (ISO timestamp, e.g. 2026-07-31T09:00:00)")
    end = _ask_timestamp("To   (ISO timestamp)")
    _show(read_between(start, end))


def count():
    print(f"  Total records: {count_metrics()}")


def create():
    """A hand-entered sample.

    Real rows come from the collector; this exists so create is
    demonstrable without waiting for one, and so a gap can be patched by
    hand. `ts` defaults to now in the same ISO format the collector writes.
    """
    record = {
        "ts": _ask_timestamp("Timestamp",
                             datetime.now().isoformat(timespec="seconds")),
        "cpu_percent": _ask_float("CPU percent"),
        "mem_percent": _ask_float("Memory percent"),
        "mem_used_mb": _ask_float("Memory used (MB)"),
        "disk_read_mb_s": _ask_float("Disk read (MB/s)", 0.0),
        "disk_write_mb_s": _ask_float("Disk write (MB/s)", 0.0),
    }
    new_id = create_metric(record)
    if new_id:
        print(f"  Created record {new_id}.")
    else:
        print("  Create failed.")


def update():
    record_id = _ask_int("Record ID")
    print(f"  Editable fields: {', '.join(sorted(EDITABLE_FIELDS))}")
    field = _ask("Field")
    if field not in EDITABLE_FIELDS:
        # Checked here as well as in update_metric so the operator is told
        # before being asked for a value they are about to throw away.
        print(f"  '{field}' is not editable. Nothing changed.")
        return
    value = _ask_float("New value")
    # ASCII only in anything that reaches the screen: run.bat drives this
    # from cmd.exe, whose code page renders a UTF-8 em-dash as a replacement
    # character mid-sentence.
    print("  Updated." if update_metric(metric_id=record_id, field=field,
                                        value=value)
          else "  Update failed - no record with that ID.")


def delete():
    record_id = _ask_int("Record ID")
    print("  Deleted." if delete_metric(record_id)
          else "  Delete failed - no record with that ID.")


def purge():
    cutoff = _ask_timestamp("Delete every record BEFORE (ISO timestamp)")
    removed = purge_before(cutoff)
    print(f"  Purged {removed if removed is not None else 0} record(s).")


# ----------------------------------------------------------------------
# Dispatch
#
# CRUD on the logged values, and nothing else. Exporting is not here
# because it is no longer something to choose: every write below mirrors
# itself into data/exports/metrics_raw.csv on the way through
# `crud.metrics_crud`. Running the pipeline is not here either — it acts
# on this data rather than being one of the operations over it.
# ----------------------------------------------------------------------
OPERATIONS = [
    ("view",    "View all records",                  view_all),
    ("latest",  "View latest N records",             view_latest),
    ("between", "View records between two times",    view_between),
    ("count",   "Total record count",                count),
    ("create",  "Create a record",                   create),
    ("update",  "Update a field on a record",        update),
    ("delete",  "Delete a record",                   delete),
    ("purge",   "Purge records before a timestamp",  purge),
]

_BY_NAME = {name: fn for name, _, fn in OPERATIONS}


def run(name):
    """Run one operation by name. Returns True if the name was known."""
    fn = _BY_NAME.get(name)
    if fn is None:
        return False
    try:
        fn()
    except _Abort:
        print("  Cancelled.")
    return True


def menu():
    """The interactive loop, used by `main.py`."""
    while True:
        print("\n---------- Data / CRUD ----------")
        print(f"  rows in metrics: {count_metrics()}")
        for i, (_, label, _fn) in enumerate(OPERATIONS, start=1):
            print(f"{i}. {label}")
        print(f"{len(OPERATIONS) + 1}. Back")

        try:
            choice = input("Enter your choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if choice == str(len(OPERATIONS) + 1):
            return
        if choice.isdigit() and 1 <= int(choice) <= len(OPERATIONS):
            run(OPERATIONS[int(choice) - 1][0])
        else:
            print("Invalid choice.")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        menu()
        return 0
    if not run(argv[0]):
        print(f"Unknown operation '{argv[0]}'.")
        print("Valid:", ", ".join(name for name, _, _ in OPERATIONS))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

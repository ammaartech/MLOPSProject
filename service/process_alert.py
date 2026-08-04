"""
Redundant-process alert — a suggestion, never an action.

When live memory crosses the configured capacity threshold, the question
an operator actually has is "what is holding it?", and the allocation
recommendation cannot answer that: it works in percentages of a node, and
a node does not tell you that a stale build server is sitting on 4 GB.
This reads the top memory consumers and files them as a recommendation
row for a human to act on.

It suggests. It does not remediate.
----------------------------------
There is no code path here that calls `kill()`, `terminate()`, `send_signal()`
or `suspend()`, and that is a design decision rather than an omission. The
system's whole argument is that a measured recommendation beats a
confident one — it rejects its own models for failing cross-validation.
A component that acted on a threshold crossing would be doing the exact
thing the rest of the project refuses to do: taking an irreversible action
on one reading. Killing the wrong process costs more than any allocation
saving this system has ever measured.

If auto-remediation is ever wanted it is a separate, opt-in feature with
its own config key, its own audit trail and its own explicit confirmation
— not a flag added here.

Source of truth
---------------
The memory reading comes from `feature_online`, not a fresh `psutil` call.
The online feature store is what the predictor serves from, so an alert
raised against a different number than the one the model saw would be
reporting on a moment that never existed anywhere else in the system. The
process list is necessarily live — that is the part that cannot be
historical.
"""

import json
from datetime import datetime, timedelta

import psutil

import config
from crud.query import execute_query

ALERT_TYPE = "process_alert"


def _recent_alert_at():
    """When this alert last fired, or None."""
    rows = execute_query(
        "SELECT ts FROM recommendations WHERE type = ? "
        "ORDER BY id DESC LIMIT 1",
        (ALERT_TYPE,), fetch=True,
    )
    if not rows:
        return None
    try:
        return datetime.fromisoformat(rows[0][0])
    except (TypeError, ValueError):
        return None


def _in_cooldown(now):
    """True while the previous alert is still recent enough to stand.

    Memory pressure persists — that is what makes it worth reporting — so
    without a cooldown the scheduler files an identical top-five list on
    every single cycle and buries the recommendations table under one
    event repeated a hundred times. The drift monitor has the same guard
    for the same reason.
    """
    cooldown = config.get_int("policy.process_alert_cooldown_sec")
    if cooldown <= 0:
        return False
    last = _recent_alert_at()
    return last is not None and (now - last) < timedelta(seconds=cooldown)


def top_processes(limit=None):
    """The heaviest processes by resident set size, largest first.

    RSS rather than a percentage: the reader is deciding what to stop, and
    "3.4 GB" is actionable where "9.8%" needs a second lookup. Processes
    that vanish or refuse to answer mid-scan are skipped — on Windows a
    good number of system processes deny access, and one of them must not
    take the whole alert down.
    """
    limit = limit or config.get_int("policy.process_alert_top_n")

    found = []
    for process in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = process.info
            memory = info.get("memory_info")
            if memory is None:
                continue
            found.append({
                "pid": info.get("pid"),
                "name": info.get("name") or "?",
                "rss_mb": round(memory.rss / (1024 ** 2), 2),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    found.sort(key=lambda p: p["rss_mb"], reverse=True)
    return found[:limit]


def live_memory_percent():
    """Current `mem_percent` as the online feature store holds it."""
    from model.feature_store import get_online_features

    frame, meta = get_online_features("mem_percent")
    if frame is None:
        return None, meta.get("error", "online store empty")

    column = "mem_percent_lag_0"
    if column not in frame.columns:
        return None, (f"{column} absent from the online vector; "
                      f"refresh_online() has not run for mem_percent")
    try:
        return float(frame[column].iloc[0]), None
    except (TypeError, ValueError) as exc:
        return None, f"unreadable {column}: {exc}"


def check(verbose=True):
    """One evaluation. Returns the alert dict, or None when nothing fired.

    Never raises into the scheduler: an alert that cannot be produced must
    not stop the allocation pipeline it runs alongside.
    """
    try:
        threshold = config.get_float("policy.capacity_alert_threshold")
        observed, error = live_memory_percent()

        if observed is None:
            if verbose:
                print(f"  [process_alert] skipped: {error}")
            return None

        if observed <= threshold:
            return None

        now = datetime.now()
        if _in_cooldown(now):
            if verbose:
                print(f"  [process_alert] mem {observed:.1f}% over "
                      f"{threshold:.1f}% — within cooldown, not refiled")
            return None

        processes = top_processes()
        payload = json.dumps(processes)

        # Two columns of `recommendations` carry different meanings for a
        # process_alert row than for an allocation row, which is the cost
        # of a type discriminator over a second table:
        #   breach_rate     the utilisation that crossed, not a rate
        #   wanted_percent  the threshold it crossed
        # `type` is what tells them apart, and every reader filters on it.
        execute_query(
            """
            INSERT INTO recommendations
                (ts, target, wanted_percent, breach_rate, type, process_payload)
            VALUES (?, 'mem_percent', ?, ?, ?, ?)
            """,
            (now.isoformat(timespec="seconds"), threshold, observed,
             ALERT_TYPE, payload),
        )

        if verbose:
            leader = processes[0] if processes else None
            summary = (f"{leader['name']} at {leader['rss_mb']} MB"
                       if leader else "no readable processes")
            print(f"  [process_alert] mem {observed:.1f}% over "
                  f"{threshold:.1f}% — top: {summary}")

        return {"observed": observed, "threshold": threshold,
                "processes": processes, "ts": now}

    except Exception as exc:                                   # noqa: BLE001
        # The scheduler positions this after predict and before drift
        # specifically so it cannot affect either. Swallowing here is what
        # makes that guarantee true rather than aspirational.
        print(f"  [process_alert] {type(exc).__name__}: {exc}")
        return None


def recent(limit=10):
    """Recent process alerts, newest first, for the dashboard."""
    rows = execute_query(
        "SELECT ts, wanted_percent, breach_rate, process_payload "
        "FROM recommendations WHERE type = ? ORDER BY id DESC LIMIT ?",
        (ALERT_TYPE, int(limit)), fetch=True,
    )
    return rows or []


def format_alert(alert):
    if alert is None:
        return "  no process alert"
    lines = [
        f"  memory {alert['observed']:.1f}% crossed "
        f"{alert['threshold']:.1f}% at {alert['ts']:%H:%M:%S}",
        f"  {'PID':>8}  {'RSS (MB)':>10}  process",
    ]
    for process in alert["processes"]:
        lines.append(f"  {process['pid']:>8}  {process['rss_mb']:>10.2f}  "
                     f"{process['name']}")
    lines.append("  Suggestion only — nothing was stopped.")
    return "\n".join(lines)


if __name__ == "__main__":
    result = check()
    if result is not None:
        print(format_alert(result))
    else:
        # `check()` returns None for three different reasons and has
        # already said which on stdout. Only the quiet one — nothing is
        # wrong and nothing crossed — needs a line here, or the summary
        # contradicts the message immediately above it.
        current, why = live_memory_percent()
        limit = config.get_float("policy.capacity_alert_threshold")
        if current is None:
            print(f"  no reading: {why}")
        elif current <= limit:
            print(f"  memory {current:.1f}% is at or below the {limit:.1f}% "
                  f"threshold — nothing to report")

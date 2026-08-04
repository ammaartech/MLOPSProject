"""
The synthetic scenario twin — constructed load, real pipeline.

    python -m collector.scenario_generator --scenario regime_change \
        --db data/metrics_twin_regime_change.db

Every number this project reports comes from one laptop over a few hours.
That is honest but narrow: it cannot say whether the finding — that the
reactive P95 policy beats the model-driven one — is a property of the
policy or an accident of this host's load. This module builds load shapes
the collector never happened to see, writes them into the SAME `metrics`
table with the same columns and units `collector/psutil_logger.py` uses,
and lets `orchestration/run_twin.py` replay the unmodified pipeline over
them.

Five shapes, each reproducing something the real data taught us:

    regime_change     flat idle, then a step to sustained load. This is
                      CV fold 0 — the fold where the model scored 16.2
                      against persistence's 4.3.
    sustained_spike   a ramp past 90%, held, then released. Exercises the
                      capacity alert threshold.
    gap_injection     a deliberate collection break. The real data has
                      three, the longest 26.7 minutes, and segment-aware
                      lags exist because of them.
    cadence_drift     the sampling interval moves 3s -> 5s -> 4s. The real
                      series is mixed-cadence, which is why cleaning has
                      to bucket rather than reindex.
    multi_host_shift  the same shape on a different baseline — the
                      cheapest available answer to "you only have one
                      host".

What this is NOT
----------------
Evidence about the real machine. A synthetic series can only test whether
the pipeline and the policy behave sensibly on a shape; it cannot make a
measured saving larger. Scenario parameters live in the config table
precisely so nobody can quietly retune them until a scenario says
something flattering — a change to any of them lands in `config_history`
with a timestamp.

Isolation
---------
`--db` is required to name a database that is not production, and the
target file is rebuilt from scratch on every run. Writes go through
`crud.query.execute_many`, the same one-transaction bulk path the ETL
load step uses.
"""

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone

# Named here so `--scenario` rejects a typo instead of silently generating
# nothing, and so `orchestration.run_twin --all` has one list to iterate.
SCENARIOS = (
    "regime_change",
    "sustained_spike",
    "gap_injection",
    "cadence_drift",
    "multi_host_shift",
)


# ----------------------------------------------------------------------
# Shape helpers
# ----------------------------------------------------------------------
def _band(rng, bounds):
    """A sample from an inclusive [low, high] band."""
    low, high = float(bounds[0]), float(bounds[1])
    return rng.uniform(low, high)


def _clamp_percent(value):
    return max(0.0, min(100.0, float(value)))


def _row(rng, ts, cpu, mem, disk_band, ram_gb):
    """One `metrics` row, in the collector's own column order and units.

    `mem_used_mb` is derived from the reference node's RAM rather than a
    literal, so changing `node.ram_gb` moves the synthetic megabytes with
    it instead of leaving them disagreeing with the percentage.
    """
    cpu = _clamp_percent(cpu)
    mem = _clamp_percent(mem)
    return (
        ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        round(cpu, 2),
        round(mem, 2),
        round((mem / 100.0) * ram_gb * 1024.0, 2),
        round(max(0.0, _band(rng, disk_band)), 2),
        round(max(0.0, _band(rng, disk_band)), 2),
    )


# ----------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------
# Each takes the resolved settings and returns a list of `metrics` rows.
# They are pure functions of (settings, seed): the same configuration
# regenerates the same series, which is what makes a twin comparison
# across two code revisions mean anything.

def generate_regime_change(s):
    """Idle for the first half, then a step change to sustained load.

    The step is instantaneous on purpose. A model trained on the idle half
    has never seen the second one, which is exactly the situation that
    produced the worst fold in the real cross-validation.
    """
    rng = random.Random(s["seed"])
    rows = []
    total = int((s["duration_min"] * 60) / s["cadence_sec"])
    switch = total // 2
    start = s["start_time"]

    for i in range(total):
        ts = start + timedelta(seconds=i * s["cadence_sec"])
        phase = s["levels"]["idle"] if i < switch else s["levels"]["high"]
        rows.append(_row(rng, ts, _band(rng, phase["cpu"]),
                         _band(rng, phase["mem"]), s["disk_low"], s["ram_gb"]))
    return rows


def generate_sustained_spike(s):
    """Low, ramp, sustained saturation above 90%, then release.

    The hold is what the capacity alert is for: a single sample over the
    threshold is noise, a held one is a capacity decision.
    """
    rng = random.Random(s["seed"])
    rows = []
    total = int((s["duration_min"] * 60) / s["cadence_sec"])
    hold = int((s["spike_hold_min"] * 60) / s["cadence_sec"])

    remaining = max(0, total - hold)
    low = int(remaining * 0.5)
    ramp = int(remaining * 0.25)
    release = max(1, remaining - low - ramp)

    idle = s["levels"]["idle"]
    top = s["levels"]["saturated"]
    start = s["start_time"]

    for i in range(total):
        ts = start + timedelta(seconds=i * s["cadence_sec"])

        if i < low:
            cpu, mem = _band(rng, idle["cpu"]), _band(rng, idle["mem"])
        elif i < low + ramp:
            # Linear interpolation from the idle band's floor to the
            # saturated band's floor, plus the same jitter the flat
            # phases carry, so the ramp is not visibly smoother than
            # everything around it.
            progress = (i - low) / max(1, ramp)
            cpu = idle["cpu"][0] + progress * (top["cpu"][0] - idle["cpu"][0])
            mem = idle["mem"][0] + progress * (top["mem"][0] - idle["mem"][0])
            cpu += rng.uniform(-2.0, 2.0)
            mem += rng.uniform(-2.0, 2.0)
        elif i < low + ramp + hold:
            cpu, mem = _band(rng, top["cpu"]), _band(rng, top["mem"])
        else:
            progress = (i - (low + ramp + hold)) / release
            cpu = top["cpu"][0] - progress * (top["cpu"][0] - idle["cpu"][0])
            mem = top["mem"][0] - progress * (top["mem"][0] - idle["mem"][0])
            cpu += rng.uniform(-2.0, 2.0)
            mem += rng.uniform(-2.0, 2.0)

        rows.append(_row(rng, ts, cpu, mem, s["disk_low"], s["ram_gb"]))
    return rows


def generate_gap_injection(s):
    """A steady series with a collection break cut out of the middle.

    Nothing marks the gap. It is simply absent, which is how the real ones
    arrived — the collector stopped and the timestamps jump. The point is
    to confirm `pipeline/clean.py` still splits the series into segments
    and that no lag or rolling window reaches across the break.
    """
    rng = random.Random(s["seed"])
    rows = []
    total = int((s["duration_min"] * 60) / s["cadence_sec"])
    start = s["start_time"]

    gap_start = start + timedelta(minutes=s["duration_min"] * 0.4)
    gap_end = gap_start + timedelta(minutes=s["gap_minutes"])

    steady = s["levels"]["steady"]
    for i in range(total):
        ts = start + timedelta(seconds=i * s["cadence_sec"])
        if gap_start <= ts <= gap_end:
            continue
        rows.append(_row(rng, ts, _band(rng, steady["cpu"]),
                         _band(rng, steady["mem"]), s["disk_low"], s["ram_gb"]))
    return rows


def generate_cadence_drift(s):
    """The same signal sampled at three different intervals in turn.

    `pipeline/clean.py` buckets with `resample` rather than reindexing
    onto a fixed grid because of this shape: a `reindex` onto a 3s
    date_range silently discards every timestamp that falls off it, which
    on the real data was a third of the rows.
    """
    rng = random.Random(s["seed"])
    rows = []
    start = s["start_time"]
    end = start + timedelta(minutes=s["duration_min"])

    cadences = [float(c) for c in s["cadence_drift_sec"]] or [s["cadence_sec"]]
    phase_len = s["duration_min"] / len(cadences)

    steady = s["levels"]["steady"]
    current = start
    while current < end:
        elapsed_min = (current - start).total_seconds() / 60.0
        index = min(int(elapsed_min / phase_len), len(cadences) - 1)
        rows.append(_row(rng, current, _band(rng, steady["cpu"]),
                         _band(rng, steady["mem"]), s["disk_low"], s["ram_gb"]))
        current += timedelta(seconds=cadences[index])
    return rows


def generate_multi_host_shift(s):
    """`regime_change`'s shape lifted onto a busier machine's baseline.

    The shape is identical and only the level moves, which is the whole
    question: does a policy calibrated on an idle-ish laptop still hold
    when the floor is 45% instead of 10%?

    Schema follow-up, stated rather than forced
    -------------------------------------------
    The brief asks for a `host_label` on each row. `metrics` has no such
    column, and adding one is a migration against the table holding the
    project's only irreplaceable data — the collected samples. That is a
    deliberate decision recorded in the TODO ("Add a `host` column and a
    second collector"), not an oversight, and it is not something a
    scenario generator should decide unilaterally. Until that column
    exists, the second host is represented by its level and the run is
    identified by the `scenario` column of `twin_runs`.
    """
    rng = random.Random(s["seed"])
    rows = []
    total = int((s["duration_min"] * 60) / s["cadence_sec"])
    switch = total // 2
    start = s["start_time"]

    for i in range(total):
        ts = start + timedelta(seconds=i * s["cadence_sec"])
        phase = s["levels"]["moderate"] if i < switch else s["levels"]["high"]
        # A busier host also moves more data, so the disk band shifts with
        # the CPU band rather than staying at the idle machine's figure.
        rows.append(_row(rng, ts, _band(rng, phase["cpu"]),
                         _band(rng, phase["mem"]), s["disk_high"], s["ram_gb"]))
    return rows


GENERATORS = {
    "regime_change": generate_regime_change,
    "sustained_spike": generate_sustained_spike,
    "gap_injection": generate_gap_injection,
    "cadence_drift": generate_cadence_drift,
    "multi_host_shift": generate_multi_host_shift,
}


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------
def resolve_settings(scenario, overrides=None):
    """Every knob a scenario uses, read from the config table.

    Called only after the database path is bound, because `config` reads
    through the database and the database is the twin's, not production's.
    """
    import config

    overrides = overrides or {}
    levels = config.get_json("twin.levels")
    disk = config.get_json("twin.disk_band_mb_s")

    duration = overrides.get("duration_min") or config.get_float("twin.duration_min")
    gap = overrides.get("gap_minutes") or config.get_float("twin.gap_minutes")

    # A gap has to fit inside the run with usable series on both sides of
    # it, or the scenario produces one segment and tests nothing. The
    # padding is the same figure on each side.
    if scenario == "gap_injection":
        minimum = gap + 2 * config.get_float("twin.gap_padding_min")
        if duration < minimum:
            print(f"  duration {duration:g} min is too short for a "
                  f"{gap:g} min gap; extending to {minimum:g} min")
            duration = minimum

    return {
        "scenario": scenario,
        "duration_min": duration,
        "gap_minutes": gap,
        "cadence_sec": (overrides.get("cadence_sec")
                        or config.get_float("pipeline.nominal_cadence_sec")),
        "cadence_drift_sec": config.get_json("twin.cadence_drift_sec"),
        "spike_hold_min": (overrides.get("spike_hold_min")
                           or config.get_float("twin.spike_hold_min")),
        "levels": levels,
        "disk_low": disk["low"],
        "disk_high": disk["high"],
        "ram_gb": config.get_float("node.ram_gb"),
        "seed": overrides.get("seed") or config.get_int("twin.seed"),
        # Scenarios end at "now" and run backwards, so the freshest sample
        # is current. `serving/predictor.py` forecasts from the last row,
        # and a series that ended two days ago is not a live signal.
        "start_time": (datetime.now(timezone.utc).replace(microsecond=0)
                       - timedelta(minutes=duration)),
    }


# ----------------------------------------------------------------------
# Target database
# ----------------------------------------------------------------------
def bind_database(db_path):
    """Point this process at the twin database and rebuild it empty.

    The environment variable has to be set before `config` is imported:
    `config.DB_PATH` is resolved once, at module load, and
    `database.connection` binds it by value. Every import in this module
    is therefore deferred until after this call.
    """
    from orchestration.twin_paths import assert_not_production, isolate_artifacts

    db_path = os.path.abspath(db_path)
    assert_not_production(db_path)

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    os.environ["RESOURCE_MONITOR_DB"] = db_path
    isolate_artifacts(db_path)

    # Rebuilt, not appended to. A scenario is a statement about one series;
    # generating twice and leaving both in place would silently double the
    # data and invent a gap between the runs.
    for suffix in ("", "-wal", "-shm", "-journal"):
        stale = db_path + suffix
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except OSError as exc:
                raise SystemExit(
                    f"Cannot rebuild {stale}: {exc}\n"
                    f"  Something still holds the file open — close the "
                    f"dashboard tab reading this scenario, or pick another "
                    f"--db path."
                ) from exc

    from database.connection import get_connection, reset_init_flag

    reset_init_flag()
    connection = get_connection()
    if connection is None:
        raise SystemExit(f"Could not open or initialise {db_path}")
    connection.close()
    return db_path


def write_rows(rows):
    """Bulk-insert into `metrics`, one transaction, parameterised."""
    from crud.query import execute_many, execute_query

    inserted = execute_many(
        """
        INSERT INTO metrics
            (ts, cpu_percent, mem_percent, mem_used_mb,
             disk_read_mb_s, disk_write_mb_s)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    verify = execute_query("SELECT COUNT(*) FROM metrics", fetch=True)
    return inserted, (verify[0][0] if verify else 0)


def generate(scenario, db_path, overrides=None, verbose=True):
    """Build one scenario into `db_path`. Returns a summary dict."""
    if scenario not in GENERATORS:
        raise ValueError(f"unknown scenario '{scenario}'; "
                         f"choose from {', '.join(SCENARIOS)}")

    db_path = bind_database(db_path)
    settings = resolve_settings(scenario, overrides)
    rows = GENERATORS[scenario](settings)

    if not rows:
        raise SystemExit(
            f"Scenario '{scenario}' produced no rows. Check twin.duration_min "
            f"and pipeline.nominal_cadence_sec."
        )

    inserted, total = write_rows(rows)

    if verbose:
        span = (settings["duration_min"], settings["cadence_sec"])
        print(f"  scenario   : {scenario}")
        print(f"  database   : {db_path}")
        print(f"  shape      : {span[0]:g} min at {span[1]:g}s nominal cadence")
        print(f"  seed       : {settings['seed']}  (same seed, same series)")
        print(f"  generated  : {len(rows)} rows")
        print(f"  inserted   : {inserted}   metrics table now holds {total}")

    return {"scenario": scenario, "db": db_path, "rows": len(rows),
            "inserted": inserted, "total": total, "settings": settings}


# ----------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate a synthetic load scenario into a twin database.")
    parser.add_argument("--scenario", required=True, choices=SCENARIOS,
                        help="which load shape to build")
    parser.add_argument("--db", default="data/metrics_twin.db",
                        help="target SQLite file; must not be production")
    parser.add_argument("--duration", type=float, default=None,
                        help="minutes of data (default: config twin.duration_min)")
    parser.add_argument("--cadence", type=float, default=None,
                        help="nominal seconds between samples "
                             "(default: config pipeline.nominal_cadence_sec)")
    parser.add_argument("--hold", type=float, default=None,
                        help="sustained_spike hold in minutes "
                             "(default: config twin.spike_hold_min)")
    parser.add_argument("--gap", type=float, default=None,
                        help="gap_injection break in minutes "
                             "(default: config twin.gap_minutes)")
    parser.add_argument("--seed", type=int, default=None,
                        help="override config twin.seed")
    args = parser.parse_args(argv)

    # The production guard refuses by raising. That is a message to
    # whoever typed the path, so it is printed as one rather than as a
    # traceback that buries the reason under a call stack.
    try:
        generate(args.scenario, args.db, overrides={
            "duration_min": args.duration,
            "cadence_sec": args.cadence,
            "spike_hold_min": args.hold,
            "gap_minutes": args.gap,
            "seed": args.seed,
        })
    except (RuntimeError, ValueError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

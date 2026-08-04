"""
Replay the real pipeline against a synthetic scenario.

    python -m orchestration.run_twin --scenario regime_change --generate
    python -m orchestration.run_twin --all

The twin answers one question the collected data cannot: is the measured
finding — reactive P95 beats the model-driven policy while staying inside
the SLA — a property of the policy, or an accident of this host? It runs
the UNMODIFIED stages against a constructed series and scores them the way
`evaluation/backtest.py` scores production, so the two tables are directly
comparable.

    scenario_generator -> etl (validate, clean, transform)
                       -> features -> forecast -> promotion gate
                       -> recommender -> walk-forward backtest

Nothing in `pipeline/`, `model/` or `service/recommender.py` is aware this
is a rehearsal, which is the point: a twin that runs different code proves
nothing about the code that runs.

Both outcomes are results
------------------------
If reactive P95 still wins, the finding survives shapes the host never
produced. If a scenario flips the ranking, that is a genuine limit on the
claim and belongs in the write-up. The scenario parameters live in the
config table (`twin.*`) so that tuning them until the answer is flattering
would leave a dated trail in `config_history`. Do not tune them.

Isolation
---------
Everything a twin writes — the database, the .joblib files, the MLflow
store, the stream watermark — is redirected under `data/twin_artifacts/`
by `orchestration/twin_paths.py` BEFORE the first `import config`. See
that module for why the ordering is load-bearing.
"""

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

from collector.scenario_generator import SCENARIOS

DEFAULT_DB_TEMPLATE = os.path.join("data", "metrics_twin_{scenario}.db")

TWIN_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS twin_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario           TEXT NOT NULL,
    timestamp          TEXT NOT NULL,
    policy             TEXT NOT NULL,
    dollars_per_month  REAL,
    worst_breach_pct   REAL,
    saving_pct         REAL,
    sla_met            INTEGER,
    model_gate_result  TEXT
)
"""


def _utf8_stdio():
    """Survive being run with stdout piped on a legacy Windows codepage.

    The dashboard runs this module with `capture_output=True`, which makes
    stdout a pipe. A pipe takes its encoding from the locale rather than
    the console, so on a cp1252 machine a single non-ASCII character in a
    report line raises UnicodeEncodeError and the run dies AFTER doing all
    of its work — with the results already written and the caller shown
    nothing but a traceback.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# ----------------------------------------------------------------------
# Binding the process to the twin database
# ----------------------------------------------------------------------
def bind(db_path):
    """Point this process at the twin, or refuse to continue.

    The plain `assert` restates the guard at the top of the file that
    depends on it; `assert_not_production` is what actually enforces it,
    because assert statements vanish under `python -O`.
    """
    from orchestration.twin_paths import assert_not_production, isolate_artifacts

    resolved = assert_not_production(db_path)
    assert "twin" in os.path.basename(resolved).lower(), (
        f"{resolved} is not a twin database")

    # `config` resolves DB_PATH once, at import, and `database.connection`
    # binds it by value. If something already imported config against a
    # different database, redirecting now would be silently ignored and
    # the run would write into whatever was bound first.
    if "config" in sys.modules:
        import config
        already = os.path.normcase(os.path.abspath(config.DB_PATH))
        if already != os.path.normcase(resolved):
            raise RuntimeError(
                f"config is already bound to {config.DB_PATH}; a twin run "
                f"must start in a fresh process. Use --all, or invoke this "
                f"module directly rather than importing it."
            )

    os.environ["RESOURCE_MONITOR_DB"] = resolved
    artifacts = isolate_artifacts(resolved)

    from database.connection import get_connection, reset_init_flag

    reset_init_flag()
    connection = get_connection()
    if connection is None:
        raise SystemExit(f"Could not open {resolved}")
    connection.execute(TWIN_RUNS_DDL)
    connection.commit()
    connection.close()

    import config
    config.invalidate()
    return resolved, artifacts


# ----------------------------------------------------------------------
# One scenario
# ----------------------------------------------------------------------
def run_twin(scenario, db_path, verbose=True):
    """Every stage, in order, against the twin. Returns a result dict."""
    resolved, artifacts = bind(db_path)

    from crud.query import execute_query
    from evaluation.backtest import combined, run_all
    from model.features import load_clean_frame
    from model.forecast import train_all
    from pipeline import etl
    from service.recommender import build_recommendation
    from tracking.mlflow_tracker import run_gate

    print("=" * 80)
    print(f"TWIN RUN — {scenario}")
    print("=" * 80)
    print(f"  database  : {resolved}")
    print(f"  artifacts : {artifacts}")
    print()

    # --- stages 1-6 ----------------------------------------------------
    print("[stages 1-6] ETL, validation, cleaning, transform")
    result = etl.run(None, verbose=verbose)
    if result["status"] != "success":
        reason = result.get("blocked_reason") or result.get("error") or result["status"]
        print(f"\n  ETL did not complete: {reason}")
        # A blocked run is a legitimate outcome, not a crash: the quality
        # gate refusing synthetic data is the gate working. Say so with a
        # distinct exit code so a caller can tell it apart from a bug.
        return {"scenario": scenario, "db": resolved, "status": result["status"],
                "reason": reason}

    run_id = result["run_id"]
    frame = load_clean_frame(run_id)
    print(f"\n[stage 7-10] features from {len(frame)} cleaned rows")

    # --- stages 11-12 ---------------------------------------------------
    print("\n[stage 11] training")
    trained = train_all(frame, run_id=run_id,
                        data_fingerprint=result["data_fingerprint"],
                        verbose=verbose)

    # MLflow logging is off. The twin's tracking store is redirected too,
    # so this is belt and braces — but a scenario is not an experiment
    # anyone should later mistake for a measurement of the real host.
    print("\n[stage 12] promotion gate")
    decisions = run_gate(trained, log_to_mlflow=False)
    gate = ", ".join(f"{t}: {d['verdict']}" for t, d in decisions.items())
    print(f"  {gate}")

    print("\n[stage 12] recommendation")
    build_recommendation(frame, persist=True)

    # --- scoring, identical to production --------------------------------
    print("\n[backtest] walk-forward replay")
    results = run_all(frame, use_model=False)
    node = combined(results)

    if node.empty:
        errors = {t: r.get("error") for t, r in results.items() if "error" in r}
        print("\n  The backtest produced no decisions.")
        for target, message in errors.items():
            print(f"    {target}: {message}")
        return {"scenario": scenario, "db": resolved, "status": "no_decisions",
                "reason": "; ".join(f"{t}: {m}" for t, m in errors.items()),
                "gate": gate}

    stamp = datetime.now().isoformat(timespec="seconds")
    for _, row in node.iterrows():
        execute_query(
            """
            INSERT INTO twin_runs
                (scenario, timestamp, policy, dollars_per_month,
                 worst_breach_pct, saving_pct, sla_met, model_gate_result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (scenario, stamp, row["policy"], float(row["monthly_cost"]),
             float(row["worst_breach_rate"]), float(row["savings_pct"]),
             1 if row["sla_met"] else 0, gate),
        )

    print()
    print(format_policy_table(scenario, node))
    return {"scenario": scenario, "db": resolved, "status": "success",
            "gate": gate, "node": node, "timestamp": stamp}


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------
def format_policy_table(scenario, node):
    """The README's policy table, same columns and same order."""
    lines = [
        "=" * 80,
        f"POLICY COMPARISON — {scenario}",
        "=" * 80,
        f"  {'Policy':<20} {'$/month':>10} {'Worst breach':>14} "
        f"{'Saving':>9} {'SLA met':>9}",
        "-" * 80,
    ]
    for _, row in node.iterrows():
        saving = ("-" if row["policy"] == "static_100"
                  else f"{row['savings_pct']:.2f}%")
        lines.append(
            f"  {row['policy']:<20} {row['monthly_cost']:>9.2f} "
            f"{row['worst_breach_rate']:>13.2f}% {saving:>9} "
            f"{('yes' if row['sla_met'] else 'no'):>9}"
        )
    lines.append("=" * 80)
    return "\n".join(lines)


def read_twin_runs(db_path, scenario=None):
    """Latest run per scenario from a twin database. Never opens production."""
    if not os.path.exists(db_path):
        return []
    try:
        connection = sqlite3.connect(db_path)
        cursor = connection.cursor()
        cursor.execute(
            "SELECT scenario, timestamp, policy, dollars_per_month, "
            "worst_breach_pct, saving_pct, sla_met, model_gate_result "
            "FROM twin_runs WHERE timestamp = "
            "(SELECT MAX(timestamp) FROM twin_runs) ORDER BY dollars_per_month"
        )
        rows = cursor.fetchall()
        connection.close()
    except sqlite3.Error:
        return []
    if scenario:
        rows = [r for r in rows if r[0] == scenario]
    return rows


# Neither of these can win the comparison, for opposite reasons.
#
# `static_100` is the thing being measured against — it is the
# over-provisioned default whose cost every saving is a saving FROM, so
# calling it the cheapest compliant policy would be circular.
#
# `oracle` sees the future. `evaluation/backtest.py` includes it to bound
# how much value a perfect forecast could ever add, not as a candidate.
# Letting it win makes the twin announce that the ranking flipped when
# what actually happened is that an unachievable policy was cheapest —
# which it always will be.
NOT_DEPLOYABLE = ("static_100", "oracle")


def format_comparison(all_rows):
    """One table across scenarios: does the ranking hold, or does it flip?

    The question this exists to answer is narrow — which DEPLOYABLE policy
    is the cheapest one that still meets the SLA, in each scenario — so
    that is what it reports, alongside the full grid.
    """
    lines = [
        "",
        "=" * 92,
        "ACROSS SCENARIOS — every policy, every shape",
        "=" * 92,
        f"  {'Scenario':<19}{'Policy':<21}{'$/month':>10}"
        f"{'Worst breach':>14}{'Saving':>10}{'SLA':>8}",
        "-" * 92,
    ]

    winners = {}
    for scenario, rows in all_rows.items():
        if not rows:
            lines.append(f"  {scenario:<19}(no result)")
            continue
        for row in rows:
            _, _, policy, dollars, breach, saving, sla, _ = row
            shown = "-" if policy == "static_100" else f"{saving:.2f}%"
            lines.append(
                f"  {scenario:<19}{policy:<21}{dollars:>10.2f}"
                f"{breach:>13.2f}%{shown:>10}{('yes' if sla else 'no'):>8}"
            )
        lines.append("")

        compliant = [r for r in rows
                     if r[6] and r[2] not in NOT_DEPLOYABLE]
        winners[scenario] = min(compliant, key=lambda r: r[3]) if compliant else None

    lines += [
        "=" * 92,
        "CHEAPEST SLA-COMPLIANT DEPLOYABLE POLICY PER SCENARIO",
        "(static_100 and oracle excluded — see NOT_DEPLOYABLE)",
        "=" * 92,
    ]
    for scenario, winner in winners.items():
        if winner is None:
            lines.append(f"  {scenario:<19} NO deployable policy met the SLA")
        else:
            lines.append(
                f"  {scenario:<19} {winner[2]:<21} ${winner[3]:.2f}/mo   "
                f"{winner[5]:.2f}% saved   worst breach {winner[4]:.2f}%"
            )

    named = [w[2] for w in winners.values() if w is not None]
    missed = [s for s, w in winners.items() if w is None and all_rows.get(s)]

    lines.append("-" * 92)
    if not named:
        lines.append("  No scenario produced an SLA-compliant deployable policy.")
    elif all(name == "reactive_p95" for name in named):
        lines.append(
            "  reactive_p95 — the policy that uses NO MODEL — is the cheapest\n"
            "  SLA-compliant policy in every scenario where one exists. The\n"
            "  finding from the collected host survives these synthetic shapes."
        )
    else:
        flipped = sorted({n for n in named if n != "reactive_p95"})
        lines.append(
            f"  The ranking FLIPS. {', '.join(flipped)} is the cheapest\n"
            f"  compliant policy in at least one scenario. That is a genuine\n"
            f"  limit on the finding and belongs in the write-up as one —\n"
            f"  do not retune the scenarios until it goes away."
        )

    if missed:
        lines.append(
            f"\n  NO policy met the SLA under: {', '.join(missed)}.\n"
            f"  A shape where every deployable policy breaches is a finding\n"
            f"  about the shape, not a failure of the run — it says the SLA\n"
            f"  is unreachable there without over-provisioning outright."
        )
    lines.append("=" * 92)
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Driving several scenarios
# ----------------------------------------------------------------------
def _spawn(scenario, db_path, generate):
    """Run one scenario in a fresh interpreter.

    A separate process per scenario is not defensiveness, it is the only
    correct way: `config.DB_PATH` is bound at import and cannot be
    rebound, so two scenarios in one process would both write to the
    first one's database.
    """
    if generate:
        gen = subprocess.run(
            [sys.executable, "-m", "collector.scenario_generator",
             "--scenario", scenario, "--db", db_path],
        )
        if gen.returncode != 0:
            return gen.returncode

    return subprocess.run(
        [sys.executable, "-m", "orchestration.run_twin",
         "--scenario", scenario, "--db", db_path],
    ).returncode


def run_all_scenarios(generate=True):
    all_rows, failures = {}, []

    for scenario in SCENARIOS:
        db_path = DEFAULT_DB_TEMPLATE.format(scenario=scenario)
        print("\n" + "#" * 80)
        print(f"#  {scenario}")
        print("#" * 80)
        code = _spawn(scenario, db_path, generate)
        if code != 0:
            failures.append(scenario)
        all_rows[scenario] = read_twin_runs(db_path, scenario)

    print(format_comparison(all_rows))
    if failures:
        print(f"\n  {len(failures)} scenario(s) did not complete: "
              f"{', '.join(failures)}")
        return 1
    return 0


# ----------------------------------------------------------------------
def main(argv=None):
    _utf8_stdio()

    parser = argparse.ArgumentParser(
        description="Replay the pipeline against a synthetic scenario.")
    parser.add_argument("--scenario", choices=SCENARIOS, default=None,
                        help="which scenario to replay")
    parser.add_argument("--db", default=None,
                        help="twin database (default: "
                             "data/metrics_twin_<scenario>.db). Must not be "
                             "production; enforced, not merely defaulted.")
    parser.add_argument("--generate", action="store_true",
                        help="build the scenario data first")
    parser.add_argument("--all", action="store_true",
                        help="generate and replay every scenario, then print "
                             "the cross-scenario comparison")
    parser.add_argument("--no-generate", action="store_true",
                        help="with --all, replay the existing twin databases")
    parser.add_argument("--report", action="store_true",
                        help="print the cross-scenario comparison from twin "
                             "databases already on disk, replaying nothing")
    args = parser.parse_args(argv)

    if args.report:
        rows = {s: read_twin_runs(DEFAULT_DB_TEMPLATE.format(scenario=s), s)
                for s in SCENARIOS}
        if not any(rows.values()):
            print("No twin runs on disk yet. Run: "
                  "python -m orchestration.run_twin --all")
            return 1
        print(format_comparison(rows))
        return 0

    if args.all:
        return run_all_scenarios(generate=not args.no_generate)

    if not args.scenario:
        parser.error("one of --scenario or --all is required")

    db_path = args.db or DEFAULT_DB_TEMPLATE.format(scenario=args.scenario)

    if args.generate:
        code = _spawn(args.scenario, db_path, generate=True)
        return code

    # A refusal is a message to the operator, not a defect. A traceback
    # here buries the one line that says which path was rejected and why.
    try:
        result = run_twin(args.scenario, db_path)
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 3

    return 0 if result.get("status") == "success" else 2


if __name__ == "__main__":
    sys.exit(main())

import os
import argparse
import sys

# 1. Parse arguments
parser = argparse.ArgumentParser(description="Digital Twin pipeline runner")
parser.add_argument("--db", default="data/metrics_twin.db", help="Path to twin SQLite database")
parser.add_argument("--scenario", default="regime_change", help="Name of the scenario being run")
args, unknown = parser.parse_known_args()

# 2. Set environment variable
os.environ["RESOURCE_MONITOR_DB"] = os.path.abspath(args.db)

# 3. Assertions to prevent touching the production database
db_path = os.environ["RESOURCE_MONITOR_DB"]
assert "twin" in os.path.basename(db_path).lower(), f"Assertion failed: DB path '{db_path}' must contain 'twin' in its filename."
assert not db_path.endswith("metrics.db"), f"Assertion failed: DB path '{db_path}' must not be the production metrics.db."

# 4. Imports after env variable is set so config reads the correct database path
import config
from database.connection import reset_init_flag, get_connection
from pipeline import etl
from model.features import load_clean_frame
from model.forecast import train_all
from tracking.mlflow_tracker import run_gate
from service.recommender import build_recommendation
from evaluation.backtest import run_all, combined
from datetime import datetime
from crud.query import execute_query, execute_insert


def init_twin_table():
    """Create the twin_runs table in the twin database only."""
    conn = get_connection()
    if conn is None:
        print("Failed to connect to the database to create twin_runs table.")
        sys.exit(1)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS twin_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scenario TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            policy TEXT NOT NULL,
            dollars_per_month REAL,
            worst_breach_pct REAL,
            saving_pct REAL,
            sla_met INTEGER,
            model_gate_result TEXT
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def main():
    # Force reloading and database initialisation for the twin DB
    reset_init_flag()
    config.invalidate()
    
    print(f"================================================================================")
    print(f"RUNNING TWIN RUN FOR SCENARIO: {args.scenario}")
    print(f"Database: {db_path}")
    print(f"================================================================================")
    
    # Initialize the twin_runs table
    init_twin_table()
    
    # Run the ETL pipeline
    print("\nExecuting Pipeline Stage: ETL...")
    etl_result = etl.run(None, verbose=True)
    if etl_result["status"] != "success":
        print(f"Pipeline stopped: ETL failed with status '{etl_result['status']}'")
        if etl_result.get("blocked_reason"):
            print(f"Reason: {etl_result['blocked_reason']}")
        sys.exit(1)
        
    run_id = etl_result["run_id"]
    data_fp = etl_result["data_fingerprint"]
    frame = load_clean_frame(run_id)
    
    # Run model training
    print("\nExecuting Pipeline Stage: Model Training...")
    train_results = train_all(frame, run_id=run_id, data_fingerprint=data_fp, verbose=True)
    
    # Run promotion gate (do not push to MLflow, skip model registration logging to avoid contaminating MLflow experiments)
    print("\nExecuting Pipeline Stage: Promotion Gate...")
    decisions = run_gate(train_results, log_to_mlflow=False)
    
    # Format gate results string
    gate_summary = ", ".join(f"{target}: {d['verdict']}" for target, d in decisions.items())
    print(f"Gate Verdict Summary: {gate_summary}")
    
    # Generate recommendations snapshot
    print("\nGenerating recommendations...")
    build_recommendation(frame, persist=True)
    
    # Run walk-forward backtest
    print("\nExecuting Pipeline Stage: Walk-Forward Backtest...")
    backtest_results = run_all(frame, use_model=False)
    
    # Combine backtest results into whole node metrics
    node_summary = combined(backtest_results)
    
    if node_summary.empty:
        print("Backtest produced no results.")
        sys.exit(1)
        
    # Write results to the twin_runs table
    now_str = datetime.now().isoformat()
    for _, row in node_summary.iterrows():
        execute_query(
            """
            INSERT INTO twin_runs 
            (scenario, timestamp, policy, dollars_per_month, worst_breach_pct, saving_pct, sla_met, model_gate_result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.scenario,
                now_str,
                row["policy"],
                float(row["monthly_cost"]),
                float(row["worst_breach_rate"]),
                float(row["savings_pct"]),
                1 if row["sla_met"] else 0,
                gate_summary
            )
        )
        
    # Print the policy table (README layout style)
    print("\n" + "=" * 80)
    print(f" POLICY COMPARISON TABLE FOR SCENARIO '{args.scenario}'")
    print("=" * 80)
    print(f"  {'Policy':28s} | {'$/month':10s} | {'Worst breach':12s} | {'Saving':8s} | {'SLA met':7s}")
    print("-" * 80)
    for _, row in node_summary.iterrows():
        policy_name = row["policy"]
        if policy_name == "reactive_p95":
            policy_disp = f"**{policy_name}**"
        else:
            policy_disp = policy_name
            
        cost_str = f"${row['monthly_cost']:.2f}"
        breach_str = f"{row['worst_breach_rate']:.2f}%"
        saving_str = f"{row['savings_pct']:.2f}%" if row["savings_pct"] != 0 else "—"
        sla_str = "yes" if row["sla_met"] else "no"
        
        print(f"  {policy_disp:28s} | {cost_str:>10s} | {breach_str:>12s} | {saving_str:>8s} | {sla_str:7s}")
    print("=" * 80)


if __name__ == "__main__":
    main()

import os
import sys
import argparse
from datetime import datetime, timedelta, timezone
import random


def generate_regime_change(duration_min, cadence_sec):
    """Idle-like flat signal for half of duration, then step change to high load."""
    rows = []
    total_samples = int((duration_min * 60) / cadence_sec)
    half_samples = total_samples // 2
    
    # Start time in the past
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(minutes=duration_min)
    
    for i in range(total_samples):
        ts = (start_time + timedelta(seconds=i * cadence_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if i < half_samples:
            # Idle
            cpu = round(random.uniform(5.0, 15.0), 2)
            mem = round(random.uniform(10.0, 20.0), 2)
        else:
            # Step change to high load
            cpu = round(random.uniform(80.0, 95.0), 2)
            mem = round(random.uniform(75.0, 85.0), 2)
            
        mem_used_mb = round((mem / 100.0) * 32.0 * 1024.0, 2)
        disk_read = round(random.uniform(0.01, 0.5), 2)
        disk_write = round(random.uniform(0.01, 0.5), 2)
        
        rows.append((ts, cpu, mem, mem_used_mb, disk_read, disk_write))
        
    return rows


def generate_sustained_spike(duration_min, cadence_sec, hold_duration_min=10):
    """Gradual ramp to >90% memory/CPU, held, then release."""
    rows = []
    total_samples = int((duration_min * 60) / cadence_sec)
    
    # We split total_samples into:
    # 1. Low load (30% of time)
    # 2. Ramp up (15% of time)
    # 3. Hold high load (hold_duration_min)
    # 4. Ramp down / Release (remaining time)
    
    hold_samples = int((hold_duration_min * 60) / cadence_sec)
    remaining_samples = total_samples - hold_samples
    low_samples = int(remaining_samples * 0.5)
    ramp_up_samples = int(remaining_samples * 0.25)
    ramp_down_samples = remaining_samples - low_samples - ramp_up_samples
    
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(minutes=duration_min)
    
    for i in range(total_samples):
        ts = (start_time + timedelta(seconds=i * cadence_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        if i < low_samples:
            # Low load
            cpu = round(random.uniform(10.0, 15.0), 2)
            mem = round(random.uniform(15.0, 20.0), 2)
        elif i < low_samples + ramp_up_samples:
            # Ramp up
            progress = (i - low_samples) / ramp_up_samples
            cpu = round(10.0 + progress * 80.0 + random.uniform(-2.0, 2.0), 2)
            mem = round(15.0 + progress * 75.0 + random.uniform(-2.0, 2.0), 2)
        elif i < low_samples + ramp_up_samples + hold_samples:
            # Hold high load (>90%)
            cpu = round(random.uniform(90.0, 96.0), 2)
            mem = round(random.uniform(90.0, 95.0), 2)
        else:
            # Release / Ramp down
            progress = (i - (low_samples + ramp_up_samples + hold_samples)) / ramp_down_samples
            cpu = round(90.0 - progress * 80.0 + random.uniform(-2.0, 2.0), 2)
            mem = round(90.0 - progress * 75.0 + random.uniform(-2.0, 2.0), 2)
            
        # Ensure values are within [0, 100]
        cpu = max(0.0, min(100.0, cpu))
        mem = max(0.0, min(100.0, mem))
        
        mem_used_mb = round((mem / 100.0) * 32.0 * 1024.0, 2)
        disk_read = round(random.uniform(0.01, 0.5), 2)
        disk_write = round(random.uniform(0.01, 0.5), 2)
        
        rows.append((ts, cpu, mem, mem_used_mb, disk_read, disk_write))
        
    return rows


def generate_gap_injection(duration_min, cadence_sec, gap_duration_min=20):
    """Drop 5-30 minutes of samples mid-run."""
    rows = []
    total_samples = int((duration_min * 60) / cadence_sec)
    
    # Mid-run gap: start gap at 40% of duration, end at 40% + gap_duration
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(minutes=duration_min)
    
    gap_start_min = duration_min * 0.4
    gap_end_min = gap_start_min + gap_duration_min
    
    gap_start_time = start_time + timedelta(minutes=gap_start_min)
    gap_end_time = start_time + timedelta(minutes=gap_end_min)
    
    for i in range(total_samples):
        curr_time = start_time + timedelta(seconds=i * cadence_sec)
        
        # Skip if within the gap window
        if gap_start_time <= curr_time <= gap_end_time:
            continue
            
        ts = curr_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        cpu = round(random.uniform(15.0, 30.0), 2)
        mem = round(random.uniform(20.0, 30.0), 2)
        mem_used_mb = round((mem / 100.0) * 32.0 * 1024.0, 2)
        disk_read = round(random.uniform(0.01, 0.5), 2)
        disk_write = round(random.uniform(0.01, 0.5), 2)
        
        rows.append((ts, cpu, mem, mem_used_mb, disk_read, disk_write))
        
    return rows


def generate_cadence_drift(duration_min):
    """Vary sampling interval mid-run (e.g. 3s -> 5s -> 4s)."""
    rows = []
    
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(minutes=duration_min)
    
    curr_time = start_time
    end_time = now
    
    phase_1_end = start_time + timedelta(minutes=duration_min / 3.0)
    phase_2_end = start_time + timedelta(minutes=2.0 * duration_min / 3.0)
    
    while curr_time < end_time:
        if curr_time < phase_1_end:
            cadence = 3
        elif curr_time < phase_2_end:
            cadence = 5
        else:
            cadence = 4
            
        ts = curr_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        cpu = round(random.uniform(15.0, 30.0), 2)
        mem = round(random.uniform(20.0, 30.0), 2)
        mem_used_mb = round((mem / 100.0) * 32.0 * 1024.0, 2)
        disk_read = round(random.uniform(0.01, 0.5), 2)
        disk_write = round(random.uniform(0.01, 0.5), 2)
        
        rows.append((ts, cpu, mem, mem_used_mb, disk_read, disk_write))
        curr_time += timedelta(seconds=cadence)
        
    return rows


def generate_multi_host_shift(duration_min, cadence_sec):
    """Same signal shape as regime_change but with a different baseline load (shifted baseline)."""
    rows = []
    total_samples = int((duration_min * 60) / cadence_sec)
    half_samples = total_samples // 2
    
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(minutes=duration_min)
    
    print("[WARNING] Schema does not include host_label. The multi_host_shift baseline shift is generated without a host_label tag as a schema follow-up.")
    
    for i in range(total_samples):
        ts = (start_time + timedelta(seconds=i * cadence_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if i < half_samples:
            # Different baseline (shifted to moderate load compared to idle)
            cpu = round(random.uniform(40.0, 50.0), 2)
            mem = round(random.uniform(50.0, 60.0), 2)
        else:
            # Shifted high load
            cpu = round(random.uniform(85.0, 98.0), 2)
            mem = round(random.uniform(80.0, 90.0), 2)
            
        mem_used_mb = round((mem / 100.0) * 32.0 * 1024.0, 2)
        disk_read = round(random.uniform(0.1, 1.0), 2)
        disk_write = round(random.uniform(0.1, 1.0), 2)
        
        rows.append((ts, cpu, mem, mem_used_mb, disk_read, disk_write))
        
    return rows


def main():
    parser = argparse.ArgumentParser(description="Synthetic Scenario Generator")
    parser.add_argument("--scenario", required=True, 
                        choices=["regime_change", "sustained_spike", "gap_injection", "cadence_drift", "multi_host_shift"],
                        help="Name of the scenario to generate")
    parser.add_argument("--db", default="data/metrics_twin.db",
                        help="Path to target SQLite database")
    parser.add_argument("--cadence", type=int, default=3,
                        help="Nominal sampling cadence in seconds")
    parser.add_argument("--duration", type=int, default=30,
                        help="Total duration in minutes")
    parser.add_argument("--hold-duration", type=int, default=10,
                        help="Hold duration in minutes (for sustained_spike)")
    parser.add_argument("--gap-duration", type=int, default=20,
                        help="Gap duration in minutes (for gap_injection)")
    
    args = parser.parse_args()
    
    # 1. Set environment variable BEFORE importing database/config modules
    db_path = os.path.abspath(args.db)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    os.environ["RESOURCE_MONITOR_DB"] = db_path
    
    # 2. Lazy imports so they read the environment variable
    import config
    from database.connection import get_connection, reset_init_flag
    from crud.query import execute_many, execute_query
    
    reset_init_flag()
    
    # Clear target database if exists or ensure initialized
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
            # Remove journal/WAL files too
            for suffix in ("-wal", "-journal", "-shm"):
                if os.path.exists(db_path + suffix):
                    os.remove(db_path + suffix)
        except OSError as e:
            print(f"Could not remove old DB file: {e}")
            
    conn = get_connection()
    if conn is None:
        print(f"Failed to connect to and initialize database: {db_path}")
        sys.exit(1)
    conn.close()
    
    # 3. Generate rows
    duration = args.duration
    if args.scenario == "gap_injection":
        if duration < args.gap_duration + 20:
            duration = args.gap_duration + 20
            print(f"Adjusting duration to {duration} minutes to accommodate gap of {args.gap_duration} minutes.")
            
    if args.scenario == "regime_change":
        rows = generate_regime_change(duration, args.cadence)
    elif args.scenario == "sustained_spike":
        rows = generate_sustained_spike(duration, args.cadence, args.hold_duration)
    elif args.scenario == "gap_injection":
        rows = generate_gap_injection(duration, args.cadence, args.gap_duration)
    elif args.scenario == "cadence_drift":
        rows = generate_cadence_drift(duration)
    elif args.scenario == "multi_host_shift":
        rows = generate_multi_host_shift(duration, args.cadence)
    else:
        raise ValueError(f"Unknown scenario {args.scenario}")
        
    print(f"Generated {len(rows)} rows for scenario '{args.scenario}'")
    
    # 4. Insert rows
    query = """
        INSERT INTO metrics
        (ts, cpu_percent, mem_percent, mem_used_mb, disk_read_mb_s, disk_write_mb_s)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    
    count = execute_many(query, rows)
    print(f"Successfully inserted {count} rows into {db_path}")
    
    # Verify count
    verify = execute_query("SELECT COUNT(*) FROM metrics", fetch=True)
    print(f"Metrics table count verify: {verify[0][0] if verify else 0} rows")


if __name__ == "__main__":
    main()

import os
import json
import psutil
from datetime import datetime
import config
from crud.query import execute_query, execute_insert


def check():
    """Verify live memory usage against threshold and log top memory processes if crossed.
    
    This function consults the online feature store to stay consistent with the
    system's source of truth.
    """
    threshold = config.get_float("policy.capacity_alert_threshold", 80.0)
    
    try:
        from model.feature_store import get_online_features
        frame, meta = get_online_features("mem_percent")
        if frame is None or "mem_percent_lag_0" not in frame.columns:
            # Fallback to reading raw metrics if online feature store isn't populated
            return None
        live_mem = float(frame["mem_percent_lag_0"].iloc[0])
    except Exception as e:
        print(f"  [process_alert] Error reading memory feature from online store: {e}")
        return None

    if live_mem > threshold:
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
            try:
                info = proc.info
                if info['memory_info'] is not None:
                    processes.append({
                        "pid": info['pid'],
                        "name": info['name'],
                        "rss": info['memory_info'].rss
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        # Sort by Resident Set Size (RSS) memory descending
        processes.sort(key=lambda x: x["rss"], reverse=True)
        top5 = processes[:5]

        # Structure payload with process details converted to MB
        payload = []
        for p in top5:
            payload.append({
                "pid": p["pid"],
                "name": p["name"],
                "rss_mb": round(p["rss"] / (1024 * 1024), 2)
            })

        now = datetime.now().isoformat(timespec="seconds")
        payload_json = json.dumps(payload)

        # Insert process alert into recommendations table using the new type discriminator
        execute_query(
            """
            INSERT INTO recommendations
                (ts, target, breach_rate, type, process_payload)
            VALUES (?, 'mem_percent', ?, 'process_alert', ?)
            """,
            (now, live_mem, payload_json)
        )
        print(f"  [process_alert] Live mem_percent ({live_mem}%) crossed alert threshold ({threshold}%). Process alert logged.")
        return payload
        
    return None

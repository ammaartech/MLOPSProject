from crud.query import execute_query

_EDITABLE = {
    "cpu_percent", "mem_percent", "mem_used_mb",
    "disk_read_mb_s", "disk_write_mb_s",
}


# ---------- CREATE ----------
def create_metric(record):
    query = """
        INSERT INTO metrics
        (ts, cpu_percent, mem_percent, mem_used_mb, disk_read_mb_s, disk_write_mb_s)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    values = (
        record["ts"],
        record["cpu_percent"],
        record["mem_percent"],
        record["mem_used_mb"],
        record["disk_read_mb_s"],
        record["disk_write_mb_s"],
    )
    return execute_query(query, values)


# ---------- READ ----------
def read_all():
    return execute_query(
        "SELECT * FROM metrics ORDER BY ts ASC", fetch=True
    )


def read_latest(n=10):
    return execute_query(
        "SELECT * FROM metrics ORDER BY id DESC LIMIT ?", (int(n),), fetch=True
    )


def read_between(start_ts, end_ts):
    return execute_query(
        "SELECT * FROM metrics WHERE ts BETWEEN ? AND ? ORDER BY ts ASC",
        (start_ts, end_ts), fetch=True
    )


def count_metrics():
    result = execute_query("SELECT COUNT(*) FROM metrics", fetch=True)
    return result[0][0] if result else 0


# ---------- UPDATE ----------
def update_metric(metric_id, field, value):
    if field not in _EDITABLE:          # whitelist = no injection via column name
        print("Invalid field:", field)
        return None
    return execute_query(
        f"UPDATE metrics SET {field} = ? WHERE id = ?", (value, metric_id)
    )


# ---------- DELETE ----------
def delete_metric(metric_id):
    return execute_query("DELETE FROM metrics WHERE id = ?", (metric_id,))


def purge_before(cutoff_ts):
    return execute_query("DELETE FROM metrics WHERE ts < ?", (cutoff_ts,))
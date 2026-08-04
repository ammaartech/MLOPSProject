from crud.query import execute_insert, execute_query

_EDITABLE = {
    "cpu_percent", "mem_percent", "mem_used_mb",
    "disk_read_mb_s", "disk_write_mb_s",
}


# The CSV mirror is imported lazily inside each writer. `conversion.csv_export`
# reaches back into `pipeline.sources` for a full rebuild, which imports this
# package again — a module-level import here would be circular. Deferring it
# also means a failure to load the exporter cannot stop a database write.
def _mirror_insert(record, row_id):
    try:
        from conversion.csv_export import mirror_insert

        mirror_insert(record, row_id)
    except Exception:                                         # noqa: BLE001
        pass


def _mirror_rebuild():
    try:
        from conversion.csv_export import mirror_rebuild

        mirror_rebuild()
    except Exception:                                         # noqa: BLE001
        pass


# ---------- CREATE ----------
def create_metric(record):
    """Insert one sample. Returns its new id.

    `execute_insert`, not `execute_query`: the latter reports a row count,
    so every successful insert came back as 1 and the CRUD console
    reported "created record 1" whatever the real key was. The collector
    ignores the return value either way.
    """
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
    new_id = execute_insert(query, values)
    if new_id:
        _mirror_insert(record, new_id)
    return new_id


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
    changed = execute_query(
        f"UPDATE metrics SET {field} = ? WHERE id = ?", (value, metric_id)
    )
    if changed:
        _mirror_rebuild()
    return changed


# ---------- DELETE ----------
def delete_metric(metric_id):
    removed = execute_query("DELETE FROM metrics WHERE id = ?", (metric_id,))
    if removed:
        _mirror_rebuild()
    return removed


def purge_before(cutoff_ts):
    removed = execute_query("DELETE FROM metrics WHERE ts < ?", (cutoff_ts,))
    if removed:
        _mirror_rebuild()
    return removed
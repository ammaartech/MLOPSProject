"""
Connection management and one-time database initialisation.

`get_connection()` is the only way the rest of the project opens the
database. It guarantees that, by the time you hold a connection, every
table in `database.schema` exists and the default configuration has been
seeded.

Initialisation runs once per process, not once per connection — the
previous version issued CREATE TABLE on every single call, which is
wasted work now that there are fourteen tables instead of one.
"""

import json
import sqlite3

from config import DB_PATH
from database.schema import (
    COLUMN_MIGRATIONS,
    DEFAULT_CONFIG,
    DEFINING_KEYS_REQUIRED,
    INDEXES,
    SCHEMA_CONTRACT_ROWS,
    TABLES,
)

# Flipped after the first successful init in this process.
_initialised = False


def _configure(connection):
    """Pragmas that matter when a collector writes while a dashboard reads."""
    cursor = connection.cursor()
    # WAL lets one writer and many readers proceed concurrently instead of
    # the readers blocking. The collector runs continuously, so this is not
    # a micro-optimisation — without it the dashboard intermittently fails.
    cursor.execute("PRAGMA journal_mode = WAL")
    # Wait rather than immediately raising "database is locked".
    cursor.execute("PRAGMA busy_timeout = 5000")
    cursor.close()


def apply_column_migrations(cursor):
    """Add columns declared after this database was first created.

    `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a
    column added to `schema.TABLES` never reaches a database that already
    exists. Each migration is checked against PRAGMA table_info first,
    which makes the whole pass idempotent and cheap.

    Returns the list of columns actually added, so a caller can report it.
    """
    added = []
    for table, column, definition in COLUMN_MIGRATIONS:
        cursor.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cursor.fetchall()}
        if not existing:
            # The table does not exist yet; the CREATE above will build it
            # with the column already in place.
            continue
        if column not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            added.append(f"{table}.{column}")
    return added


def merge_defining_keys(cursor):
    """Ensure newly added config keys join `features.defining_keys`.

    That list decides which settings are hashed into the feature-store
    version id. A key introduced later has to be added to it, or changing
    that key would alter the feature values while leaving the version id
    unchanged — and the train/serve compatibility check would pass on
    features that no longer match.

    This MERGES: any key the user added by hand is preserved. It is the
    one place that writes to a config value the database already owns, and
    it only ever appends.
    """
    cursor.execute("SELECT value FROM config WHERE key = 'features.defining_keys'")
    row = cursor.fetchone()
    if row is None:
        return []

    try:
        current = json.loads(row[0])
    except (TypeError, ValueError):
        return []
    if not isinstance(current, list):
        return []

    missing = [k for k in DEFINING_KEYS_REQUIRED if k not in current]
    if not missing:
        return []

    merged = current + missing
    cursor.execute(
        "UPDATE config SET value = ?, updated_at = datetime('now') "
        "WHERE key = 'features.defining_keys'",
        (json.dumps(merged),),
    )
    cursor.execute(
        """
        INSERT INTO config_history (key, old_value, new_value, changed_at, source)
        VALUES ('features.defining_keys', ?, ?, datetime('now'), 'migration')
        """,
        (row[0], json.dumps(merged)),
    )
    return missing


def init_db(connection):
    """Create every table and index, then seed defaults. Idempotent."""
    cursor = connection.cursor()

    for ddl in TABLES.values():
        cursor.execute(ddl)
    for index_sql in INDEXES:
        cursor.execute(index_sql)

    apply_column_migrations(cursor)

    # INSERT OR IGNORE: seeding never overwrites a value the database
    # already holds. Once a key exists, the database owns it and editing
    # schema.py will not change the running system.
    cursor.executemany(
        """
        INSERT OR IGNORE INTO config
            (key, value, value_type, category, description, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        """,
        DEFAULT_CONFIG,
    )

    cursor.executemany(
        """
        INSERT OR IGNORE INTO schema_contract
            (table_name, column_name, dtype, nullable,
             min_value, max_value, is_target, description)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        SCHEMA_CONTRACT_ROWS,
    )

    # After seeding, so keys introduced in this release exist before they
    # are merged into the defining-keys list.
    merge_defining_keys(cursor)

    connection.commit()
    cursor.close()


def get_connection():
    """Open a connection, initialising the database on first use."""
    global _initialised
    try:
        connection = sqlite3.connect(DB_PATH)
        _configure(connection)
        if not _initialised:
            init_db(connection)
            _initialised = True
        return connection
    except sqlite3.Error as e:
        print("Database connection failed:", e)
        return None


def reset_init_flag():
    """Force re-initialisation on the next connection.

    Only needed by tests and by tooling that points the process at a
    different database mid-run.
    """
    global _initialised
    _initialised = False

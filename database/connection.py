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

import sqlite3

from config import DB_PATH
from database.schema import (
    DEFAULT_CONFIG,
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


def init_db(connection):
    """Create every table and index, then seed defaults. Idempotent."""
    cursor = connection.cursor()

    for ddl in TABLES.values():
        cursor.execute(ddl)
    for index_sql in INDEXES:
        cursor.execute(index_sql)

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

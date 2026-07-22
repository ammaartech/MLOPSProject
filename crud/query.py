"""
The single point where SQL is executed.

Every value is bound as a parameter. No caller interpolates user input
into a statement — where a column or table name must be dynamic, the
caller validates it against a whitelist first (see `metrics_crud.update_metric`
and `pipeline.schema`).
"""

from database.connection import get_connection


def execute_query(query, values=None, fetch=False):
    """Run one statement. Returns rows when `fetch`, else the row count."""
    connection = get_connection()
    if connection is None:
        return None

    cursor = connection.cursor()
    try:
        cursor.execute(query, values or ())
        if fetch:
            result = cursor.fetchall()
        else:
            connection.commit()
            result = cursor.rowcount
        return result
    except Exception as e:
        print("SQL execution failed:", e)
        return None
    finally:
        cursor.close()
        connection.close()


def execute_insert(query, values=None):
    """Insert one row and return its new primary key.

    `last_insert_rowid()` is scoped to a connection, and `execute_query`
    opens a fresh one per call — so issuing the INSERT and then querying
    for the id as two separate calls always returns 0. They have to share
    a connection, which is what this does.
    """
    connection = get_connection()
    if connection is None:
        return None

    cursor = connection.cursor()
    try:
        cursor.execute(query, values or ())
        connection.commit()
        return cursor.lastrowid
    except Exception as e:
        print("SQL insert failed:", e)
        return None
    finally:
        cursor.close()
        connection.close()


def execute_many(query, rows):
    """Insert many rows over one connection. Returns the row count.

    A per-row `execute_query` would open and close a connection thousands
    of times; the ETL load step depends on this being one transaction.
    """
    if not rows:
        return 0

    connection = get_connection()
    if connection is None:
        return 0

    cursor = connection.cursor()
    try:
        cursor.executemany(query, rows)
        connection.commit()
        return cursor.rowcount
    except Exception as e:
        print("SQL bulk insert failed:", e)
        return 0
    finally:
        cursor.close()
        connection.close()

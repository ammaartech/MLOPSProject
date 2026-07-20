from database.connection import get_connection


def execute_query(query, values=None, fetch=False):
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
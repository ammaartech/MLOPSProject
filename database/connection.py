import sqlite3
from config import DB_PATH


def get_connection():
    try:
        connection = sqlite3.connect(DB_PATH)
        cursor = connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                cpu_percent   REAL,
                mem_percent   REAL,
                mem_used_mb   REAL,
                disk_percent  REAL,
                disk_used_gb  REAL
            )
        """)
        connection.commit()
        cursor.close()
        return connection
    except sqlite3.Error as e:
        print("Database connection failed:", e)
        return None
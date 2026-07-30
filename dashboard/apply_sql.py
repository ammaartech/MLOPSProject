"""
Applies dashboard/sql/*.sql to the Supabase Postgres database.

    .\\.venv\\Scripts\\python.exe -m dashboard.apply_sql

Everything here is also copy-pasteable into Supabase > SQL editor; this
just saves the round trip and prints the resulting account list, so a
fresh clone can prove the login works before opening the dashboard.

Needs SUPABASE_DB_URL in the environment or in `.env` — the direct
Postgres connection string from Supabase > Settings > Database. That is a
superuser credential and a different thing from the anon key the app
runs on: the app can only read one row of `profiles`, this can rewrite
the auth tables. It is deliberately not read by any other module.
"""

import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SQL_DIR = Path(__file__).resolve().parent / "sql"
FILES = ["auth_schema.sql", "seed_users.sql"]


def main():
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        print(
            "SUPABASE_DB_URL is not set. Copy .env.example to .env and fill\n"
            "it in from Supabase > Settings > Database > Connection string.\n"
            "Percent-encode any '@' in the password as %40."
        )
        return 1

    try:
        import psycopg2
    except ImportError:
        print("psycopg2-binary is not installed: pip install -r requirements.txt")
        return 1

    conn = psycopg2.connect(url, connect_timeout=20)
    conn.autocommit = True                    # each file manages its own txn
    try:
        for name in FILES:
            path = SQL_DIR / name
            with conn.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))
            print(f"applied  {name}")

        with conn.cursor() as cur:
            cur.execute(
                "select p.email, p.role, u.email_confirmed_at is not null "
                "from public.profiles p join auth.users u on u.id = p.id "
                "order by p.role, p.email"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    print(f"\n{len(rows)} account(s):")
    for email, role, confirmed in rows:
        flag = "confirmed" if confirmed else "UNCONFIRMED — cannot sign in"
        print(f"  {role:<9} {email:<28} {flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

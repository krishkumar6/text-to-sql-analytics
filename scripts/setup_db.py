"""One-shot setup for a hosted Postgres (Neon, Supabase, RDS…): roles, trace table, demo data.

    DATABASE_ADMIN_URL=postgresql://<owner>:<pw>@<host>/<db>?sslmode=require \
    python -m scripts.setup_db

Runs db/init/01_roles.sql as the database owner, sets strong random passwords on analytics_ro and
app_logger (or the ones in ANALYTICS_RO_PASSWORD / APP_LOGGER_PASSWORD), seeds the demo data, checks
that analytics_ro really cannot write, and prints the two app connection strings. Safe to re-run;
re-running rotates the passwords unless you pass them in.
"""

from __future__ import annotations

import os
import secrets
import sys
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql

from scripts import seed

ROOT = seed.ROOT


def app_url(admin_url: str, user: str, password: str) -> str:
    """Same host/db/options as the admin URL, different credentials, SQLAlchemy+psycopg scheme."""
    parts = urlsplit(admin_url)
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit(("postgresql+psycopg", f"{quote(user)}:{quote(password, safe='')}@{host}",
                       parts.path, parts.query, ""))


def main() -> None:
    admin_url = os.getenv("DATABASE_ADMIN_URL")
    if not admin_url:
        sys.exit("Set DATABASE_ADMIN_URL to the owner connection string of the target database.")
    local = urlsplit(admin_url).hostname in ("localhost", "127.0.0.1", "db")
    passwords = {
        "analytics_ro": os.getenv("ANALYTICS_RO_PASSWORD") or ("analytics_ro" if local else secrets.token_urlsafe(24)),
        "app_logger": os.getenv("APP_LOGGER_PASSWORD") or ("app_logger" if local else secrets.token_urlsafe(24)),
    }

    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute((ROOT / "db" / "init" / "01_roles.sql").read_text(encoding="utf-8"))
        for role, password in passwords.items():
            conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
    print("Roles and ops.query_traces ready.")

    os.environ["DATABASE_ADMIN_URL"] = admin_url
    seed.ADMIN_URL = admin_url
    seed.main()

    ro_url = app_url(admin_url, "analytics_ro", passwords["analytics_ro"])
    with psycopg.connect(ro_url.replace("postgresql+psycopg", "postgresql", 1)) as conn:
        try:
            conn.execute("DELETE FROM plans")
            sys.exit("SECURITY CHECK FAILED: analytics_ro was able to write.")
        except psycopg.Error as exc:
            print(f"Checked: analytics_ro cannot write ({type(exc).__name__}).")

    print("\nSet these as secrets on your host (they contain passwords; don't commit them):")
    print(f"DATABASE_URL={ro_url}")
    print(f"TRACE_DATABASE_URL={app_url(admin_url, 'app_logger', passwords['app_logger'])}")


if __name__ == "__main__":
    main()

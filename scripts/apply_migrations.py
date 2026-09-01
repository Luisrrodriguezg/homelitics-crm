#!/usr/bin/env python3
"""Apply migration files to the database, in order.

There is no Alembic (see docs/DECISIONS.md §8). Migrations are plain SQL and
this runner just executes them against the session pooler, one transaction per
file. Each migration file is written to be idempotent, so re-running is safe.

Usage:
    python scripts/apply_migrations.py                 # apply every migrations/*.sql
    python scripts/apply_migrations.py 003_availability.sql 004_events_outbox.sql
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "migrations"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return
    load_dotenv(ROOT / ".env")


def main() -> None:
    _load_dotenv()
    import psycopg2

    url = os.environ.get("DATABASE_URL_MIGRATE") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL_MIGRATE (Supabase session-pooler URI, port 5432).")

    names = sys.argv[1:] or sorted(p.name for p in MIGRATIONS.glob("*.sql"))

    conn = psycopg2.connect(url)
    conn.autocommit = False
    try:
        for name in names:
            sql = (MIGRATIONS / name).read_text()
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            print(f"applied {name}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

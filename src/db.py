"""Thin psycopg (v3) helpers. Raw SQL everywhere -- no ORM -- so the provenance
and merge queries stay readable and reviewable."""
import os

import psycopg

from . import config

MIGRATION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "migrations", "001_init.sql")


def connect(dsn: str | None = None) -> psycopg.Connection:
    return psycopg.connect(dsn or config.DB_DSN)


def run_migration(conn: psycopg.Connection) -> None:
    """Apply migrations/001_init.sql. It is fully IF NOT EXISTS, so safe to
    call on every startup."""
    with open(MIGRATION_FILE, "r", encoding="utf-8") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def tables_exist(conn: psycopg.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.candidates')")
        return cur.fetchone()[0] is not None


def ensure_schema(conn: psycopg.Connection) -> None:
    if not tables_exist(conn):
        run_migration(conn)

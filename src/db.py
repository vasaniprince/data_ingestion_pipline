"""Thin psycopg (v3) helpers. Raw SQL everywhere -- no ORM -- so the provenance
and merge queries stay readable and reviewable."""
import os

import psycopg

from . import config

MIGRATION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "migrations", "001_init.sql")


def connect(dsn: str | None = None) -> psycopg.Connection:
    return psycopg.connect(dsn or config.DB_DSN)


_MIGRATION_LOCK_KEY = 891234  # arbitrary constant, namespaces this app's advisory lock


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
    """Idempotent AND concurrency-safe: the API's own startup hook and a CLI
    command (ingest/enrich/export) can both call this within moments of each
    other right after `docker compose up`. Without serializing, two
    processes can both see "no tables yet" and race on running the
    migration -- `CREATE EXTENSION IF NOT EXISTS pg_trgm` is not safe against
    true concurrent execution and raises a duplicate-key error on Postgres's
    own catalog. A session-level advisory lock makes the second caller wait
    for the first to finish, then its own tables_exist() check sees the
    schema already applied and skips the migration entirely."""
    if tables_exist(conn):
        return
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
    try:
        if not tables_exist(conn):
            run_migration(conn)
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))

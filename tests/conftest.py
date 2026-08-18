import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src import config, db

TABLES = [
    "candidates", "identities", "field_provenance", "change_log",
    "raw_source_rows", "enrichment_cache", "run_reports",
]


@pytest.fixture
def clean_db():
    """A connection to the schema, truncated before the test runs. Uses
    DATABASE_URL if set, else src.config's default (the persistent saral_pg
    container on localhost:55432)."""
    conn = db.connect(os.environ.get("DATABASE_URL", config.DB_DSN))
    db.ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    conn.commit()
    yield conn
    conn.close()


def business_digest(conn):
    """A deterministic digest of everything that should be byte-identical
    across an idempotent re-run: every business column, explicitly excluding
    volatile audit timestamps (ingested_at, recorded_at, created_at,
    updated_at) and the append-only run_reports log."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT candidate_id, full_name, normalized_name, current_title, "
            " current_company, location_raw, location_city, location_state, "
            " location_country, experience_months, annual_salary_inr, "
            " notice_period_days, skills, open_to_work, data_quality_flags, "
            " source_count, identity_count, completeness_score, contactable, "
            " first_seen_at, last_seen_at "
            "FROM candidates ORDER BY candidate_id"
        )
        candidates = cur.fetchall()

        cur.execute(
            "SELECT candidate_id, identity_type, value, match_key, raw_forms, "
            " observed_in, email_type, confidence, is_canonical, first_seen_at "
            "FROM identities ORDER BY candidate_id, identity_type, match_key"
        )
        identities = cur.fetchall()

        cur.execute(
            "SELECT candidate_id, field_name, value, source, source_row_id, "
            " observed_at, confidence, note, reason, is_current "
            "FROM field_provenance "
            "ORDER BY candidate_id, field_name, observed_at, source_row_id"
        )
        provenance = cur.fetchall()

        cur.execute(
            "SELECT candidate_id, field_name, old_value, new_value, source, "
            " source_row_id, batch, rule, note, (applied_at IS NOT NULL) "
            "FROM change_log ORDER BY candidate_id, field_name, source_row_id, rule"
        )
        changes = cur.fetchall()

        cur.execute(
            "SELECT batch, source, source_row_id, candidate_id, rejected, "
            " rejection_reason, content_hash "
            "FROM raw_source_rows ORDER BY content_hash"
        )
        raw = cur.fetchall()

    return {
        "candidates": candidates, "identities": identities,
        "provenance": provenance, "changes": changes, "raw": raw,
    }


def change_log_count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM change_log")
        return cur.fetchone()[0]

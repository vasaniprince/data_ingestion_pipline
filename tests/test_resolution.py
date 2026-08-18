"""Assert the specific hostile cases the brief says it will check:
no false merges (two Aaravs), no missed merges (Vikram via combination rule),
gmail-only dot rule (Sneha keeps two distinct deloitte emails), and garbage
rows never landing in the table.
"""
import os

from src import config
from src.ingest import ingest_batch

BATCH01 = os.path.join(config.DATA_DIR, "batch_01_raw.jsonl")
BATCH02 = os.path.join(config.DATA_DIR, "batch_02_raw.jsonl")


def _run_both(conn):
    ingest_batch(conn, "batch_01", BATCH01)
    ingest_batch(conn, "batch_02", BATCH02)


def test_two_aaravs_never_merge(clean_db):
    conn = clean_db
    _run_both(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT candidate_id, current_company, location_city, data_quality_flags "
            "FROM candidates WHERE normalized_name='aarav mehta' ORDER BY current_company"
        )
        rows = cur.fetchall()
    assert len(rows) == 2, "the two Aarav Mehtas must remain separate candidates"
    companies = {r[1] for r in rows}
    assert companies == {"Razorpay", "Maersk"}
    for _cid, _company, _city, flags in rows:
        assert "name_collision_reviewed" in flags


def test_vikram_merges_via_combination_rule(clean_db):
    conn = clean_db
    _run_both(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT candidate_id, annual_salary_inr, notice_period_days "
            "FROM candidates WHERE normalized_name='vikram singh'"
        )
        rows = cur.fetchall()
    assert len(rows) == 1, "Vikram must resolve to exactly one candidate"
    _cid, salary, notice = rows[0]
    assert salary == 6_200_000
    assert notice == 90


def test_sneha_keeps_two_distinct_deloitte_emails(clean_db):
    conn = clean_db
    _run_both(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM identities WHERE identity_type='email' "
            "AND candidate_id=(SELECT candidate_id FROM identities "
            "  WHERE match_key='sneha-kulkarni-analyst')"
        )
        emails = {r[0] for r in cur.fetchall()}
    assert "sneha.kulkarni@deloitte.com" in emails
    assert "snehakulkarni@deloitte.com" in emails


def test_garbage_rows_rejected_not_landed(clean_db):
    conn = clean_db
    ingest_batch(conn, "batch_01", BATCH01)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_row_id, candidate_id FROM raw_source_rows "
            "WHERE source_row_id IN (20, 21)"
        )
        rows = dict(cur.fetchall())
    assert rows[20] is None
    assert rows[21] is None


def test_noop_recrawl_writes_zero_change_rows(clean_db):
    """Tanvi's batch_02 row is byte-for-byte the same facts as batch_01 --
    must produce zero change_log rows for her."""
    conn = clean_db
    _run_both(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM change_log WHERE candidate_id = "
            "(SELECT candidate_id FROM identities WHERE match_key='tanvi-shah-ai')"
        )
        count = cur.fetchone()[0]
    assert count == 0

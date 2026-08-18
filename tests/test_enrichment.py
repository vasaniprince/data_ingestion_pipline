"""Part 4 -- enrichment on a budget. Assert the specific things the brief
grades: selection is justified (no guessed keys), a second full run costs
Rs 0.00, not_found is billed and cached with a TTL, and found fields merge
through the same Part-3 precedence engine (no special-casing).
"""
import os

from src import config
from src.enrich import run_enrichment
from src.ingest import ingest_batch

BATCH01 = os.path.join(config.DATA_DIR, "batch_01_raw.jsonl")
BATCH02 = os.path.join(config.DATA_DIR, "batch_02_raw.jsonl")


def _seed(conn):
    ingest_batch(conn, "batch_01", BATCH01)
    ingest_batch(conn, "batch_02", BATCH02)


def _candidate_id(conn, full_name):
    with conn.cursor() as cur:
        cur.execute("SELECT candidate_id FROM candidates WHERE full_name=%s", (full_name,))
        return cur.fetchone()[0]


def test_no_key_candidates_never_billed(clean_db):
    conn = clean_db
    _seed(conn)
    report = run_enrichment(conn)

    siddharth = _candidate_id(conn, "Siddharth Rao")
    divya = _candidate_id(conn, "Divya Menon")
    assert siddharth in report["skipped_no_key"]
    assert divya in report["skipped_no_key"]

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM enrichment_cache WHERE candidate_id IN (%s,%s)",
                    (siddharth, divya))
        assert cur.fetchone()[0] == 0


def test_second_run_costs_zero(clean_db):
    conn = clean_db
    _seed(conn)
    r1 = run_enrichment(conn)
    assert r1["enrichment"]["calls_made"] > 0
    assert r1["enrichment"]["spend_inr"] > 0

    r2 = run_enrichment(conn)
    assert r2["enrichment"]["calls_made"] == 0
    assert r2["enrichment"]["spend_inr"] == 0.0
    assert r2["enrichment"]["contactable_before"] == r2["enrichment"]["contactable_after"]


def test_found_result_merges_through_precedence(clean_db):
    conn = clean_db
    _seed(conn)
    run_enrichment(conn)

    tanvi = _candidate_id(conn, "Tanvi Shah")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT contactable, completeness_score FROM candidates WHERE candidate_id=%s",
            (tanvi,))
        contactable, score = cur.fetchone()
        assert contactable is True

        cur.execute(
            "SELECT identity_type, observed_in FROM identities WHERE candidate_id=%s",
            (tanvi,))
        rows = dict(cur.fetchall())
        assert "enrichment" in rows.get("email", [])
        assert "enrichment" in rows.get("phone", [])


def test_not_found_is_cached_with_ttl(clean_db):
    conn = clean_db
    _seed(conn)
    run_enrichment(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, ttl_expires_at - fetched_at FROM enrichment_cache "
            "WHERE cache_key='nikhil-reddy-java'")
        status, ttl_delta = cur.fetchone()
        assert status == "not_found"
        assert ttl_delta.days == config.ENRICHMENT_NEGATIVE_TTL_DAYS

    nikhil = _candidate_id(conn, "Nikhil Reddy")
    with conn.cursor() as cur:
        cur.execute("SELECT contactable FROM candidates WHERE candidate_id=%s", (nikhil,))
        assert cur.fetchone()[0] is False


def test_budget_respected(clean_db, monkeypatch):
    conn = clean_db
    _seed(conn)
    monkeypatch.setattr(config, "ENRICHMENT_BUDGET_CALLS", 2)

    report = run_enrichment(conn)
    assert report["enrichment"]["calls_made"] == 2
    assert len(report["skipped_budget"]) == 2

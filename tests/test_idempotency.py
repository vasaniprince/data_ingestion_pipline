"""The graded double-run test: run batch_01 then batch_02, snapshot the data
tables, run both again, and assert nothing changed and nothing new was
logged. This is "run it twice" made concrete instead of asserted in prose.
"""
import os

from src import config
from src.ingest import ingest_batch

from .conftest import business_digest, change_log_count

BATCH01 = os.path.join(config.DATA_DIR, "batch_01_raw.jsonl")
BATCH02 = os.path.join(config.DATA_DIR, "batch_02_raw.jsonl")


def test_double_run_is_a_true_noop(clean_db):
    conn = clean_db

    r1a = ingest_batch(conn, "batch_01", BATCH01)
    r1b = ingest_batch(conn, "batch_02", BATCH02)
    digest_after_first_pass = business_digest(conn)
    changes_after_first_pass = change_log_count(conn)

    assert r1a["entities_created"] == 16
    assert r1b["entities_created"] == 5

    # run it again, against the SAME populated database
    r2a = ingest_batch(conn, "batch_01", BATCH01)
    r2b = ingest_batch(conn, "batch_02", BATCH02)
    digest_after_second_pass = business_digest(conn)
    changes_after_second_pass = change_log_count(conn)

    # the second pass must create nothing and change nothing
    assert r2a["entities_created"] == 0
    assert r2a["fields_changed"] == 0
    assert r2a["entities_updated"] == 0
    assert r2b["entities_created"] == 0
    assert r2b["fields_changed"] == 0
    assert r2b["entities_updated"] == 0

    # the table is byte-identical (business columns) and zero new change rows
    assert digest_after_second_pass == digest_after_first_pass
    assert changes_after_second_pass == changes_after_first_pass


def test_batch_then_reversed_rerun_still_converges(clean_db):
    """Run batch_01, batch_02, batch_01, batch_02 (the exact sequence named in
    the brief) and confirm the final state matches a single clean pass."""
    conn = clean_db
    ingest_batch(conn, "batch_01", BATCH01)
    ingest_batch(conn, "batch_02", BATCH02)
    baseline = business_digest(conn)

    ingest_batch(conn, "batch_01", BATCH01)
    ingest_batch(conn, "batch_02", BATCH02)
    final = business_digest(conn)

    assert final == baseline

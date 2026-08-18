"""Part 5 -- the FastAPI service and the CSV deliverable. Seeds the same
clean_db fixture used by every other test, then exercises /candidates,
/candidates/{id}, /stats, and the exporter against it.
"""
import csv
import os

from fastapi.testclient import TestClient

from src import config
from src.api import app
from src.enrich import run_enrichment
from src.export import COLUMNS, export_csv
from src.ingest import ingest_batch

BATCH01 = os.path.join(config.DATA_DIR, "batch_01_raw.jsonl")
BATCH02 = os.path.join(config.DATA_DIR, "batch_02_raw.jsonl")


def _seed(conn):
    ingest_batch(conn, "batch_01", BATCH01)
    ingest_batch(conn, "batch_02", BATCH02)
    run_enrichment(conn)


def _candidate_id(conn, full_name):
    with conn.cursor() as cur:
        cur.execute("SELECT candidate_id FROM candidates WHERE full_name=%s", (full_name,))
        return cur.fetchone()[0]


def test_search_filters(clean_db):
    conn = clean_db
    _seed(conn)
    client = TestClient(app)

    r = client.get("/candidates", params={"location": "bengaluru", "open_to_work": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    for c in body["results"]:
        assert c["location"]["city"] == "bengaluru"
        assert c["open_to_work"] is True

    r2 = client.get("/candidates", params={"min_experience": 100})
    assert r2.status_code == 200
    assert r2.json()["total"] == 0  # nobody has 100+ years of experience

    r3 = client.get("/candidates", params={"q": "vikram"})
    names = {c["full_name"] for c in r3.json()["results"]}
    assert "Vikram Singh" in names


def test_candidate_detail_has_provenance_and_changelog(clean_db):
    conn = clean_db
    _seed(conn)
    client = TestClient(app)
    vikram_id = _candidate_id(conn, "Vikram Singh")

    r = client.get(f"/candidates/{vikram_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["full_name"] == "Vikram Singh"
    assert len(body["identities"]) > 0
    assert "current_title" in body["field_provenance"]
    assert isinstance(body["change_log"], list)
    assert len(body["change_log"]) > 0

    r404 = client.get("/candidates/cand_doesnotexist")
    assert r404.status_code == 404


def test_stats_returns_latest_report(clean_db):
    conn = clean_db
    _seed(conn)
    client = TestClient(app)

    r = client.get("/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["batch"] == "enrichment"
    assert "enrichment" in body["report"]

    r2 = client.get("/stats", params={"batch": "batch_01"})
    assert r2.status_code == 200
    assert r2.json()["batch"] == "batch_01"


def test_csv_is_deterministic_and_correct(clean_db, tmp_path):
    conn = clean_db
    _seed(conn)

    out1 = str(tmp_path / "run1.csv")
    out2 = str(tmp_path / "run2.csv")
    export_csv(conn, out1)
    export_csv(conn, out2)

    with open(out1, "rb") as f1, open(out2, "rb") as f2:
        assert f1.read() == f2.read()

    with open(out1, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == COLUMNS
        rows = {row["full_name"]: row for row in reader}

    assert rows["Nikhil Reddy"]["enriched"] == "false"
    assert rows["Nikhil Reddy"]["enrichment_cost_inr"] == "0.09"

    assert rows["Tanvi Shah"]["enriched"] == "true"
    assert rows["Tanvi Shah"]["enrichment_cost_inr"] == "0.09"

    # both Aarav Mehtas must survive as separate rows (rows dict above
    # collapses same-name keys, so re-scan the raw rows for this check)
    with open(out1, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        aaravs = [row for row in reader if row["full_name"] == "Aarav Mehta"]
    assert len(aaravs) == 2
    for row in aaravs:
        assert "name_collision_reviewed" in row["data_quality_flags"].split(";")

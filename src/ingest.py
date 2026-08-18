"""Batch ingest orchestrator + CLI.

    python -m src.ingest batch_01      # reads data/batch_01_raw.jsonl

Pipeline per run: read jsonl -> sort by (crawled_at, source_row_id) ->
for each row: insert raw (content-hash dedupe) -> map -> reject-check ->
resolve identity + merge -> finalize canonical ids + metrics -> emit the run
report to out/ and the run_reports table. One transaction per run (fine at
this size; a 1M/day build would checkpoint in chunks -- see INFRA.md).
"""
import hashlib
import json
import os
import sys
from datetime import datetime

from . import config, db, ids
from .identity import (process_record, finalize_canonical_identities,
                       finalize_metrics)
from .mapping import map_row
from .report import RunReport

KNOWN_SOURCES = {"linkedin_scraper", "naukri_export", "github_crawl"}


def _content_hash(raw: dict) -> str:
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _read_jsonl(path):
    """Yield (raw_dict | None, raw_line). None dict = unparseable line."""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line), line
            except json.JSONDecodeError:
                yield None, line


def _insert_raw(cur, batch, source, source_row_id, crawled_at, raw, content_hash,
                rejected=False, reason=None):
    cur.execute(
        "INSERT INTO raw_source_rows (batch, source, source_row_id, crawled_at, "
        " raw_payload, content_hash, rejected, rejection_reason) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (content_hash) DO NOTHING RETURNING id",
        (batch, source, source_row_id, crawled_at, json.dumps(raw),
         content_hash, rejected, reason),
    )
    row = cur.fetchone()
    return row[0] if row else None


def ingest_batch(conn, batch: str, path: str) -> dict:
    report = RunReport(batch)
    cur = conn.cursor()

    # parse + sort (deterministic, chronological for correct first/last-seen)
    parsed = list(_read_jsonl(path))
    report.rows_in = len(parsed)

    def sort_key(item):
        raw, _ = item
        if not raw:
            return ("", 0)
        return (raw.get("_crawled_at") or "", raw.get("_row") or 0)

    parsed.sort(key=sort_key)

    created = set()
    preexisting_touched = set()
    changes_by_cand = {}
    attach_rule = {}

    for raw, line in parsed:
        # unparseable -> keep raw, reject, never process
        if raw is None:
            ch = hashlib.sha256(line.encode("utf-8")).hexdigest()
            _insert_raw(cur, batch, "unknown", None, None, {"_raw_line": line}, ch,
                        rejected=True, reason="unparseable_json")
            report.reject("unparseable_json")
            continue

        source = raw.get("_src")
        ch = _content_hash(raw)
        raw_id = _insert_raw(cur, batch, source or "unknown", raw.get("_row"),
                             raw.get("_crawled_at"), raw, ch)
        if raw_id is None:
            # identical row already ingested -> idempotent skip (no reprocessing)
            continue

        if source not in KNOWN_SOURCES:
            cur.execute("UPDATE raw_source_rows SET rejected=TRUE, rejection_reason=%s "
                        "WHERE id=%s", ("unknown_source", raw_id))
            report.reject("unknown_source")
            continue

        rec = map_row(raw, batch)
        rec.source_row_pk = raw_id

        if not rec.has_usable_identifier:
            cur.execute("UPDATE raw_source_rows SET rejected=TRUE, rejection_reason=%s "
                        "WHERE id=%s", ("no_usable_identifier", raw_id))
            report.reject("no_usable_identifier")
            continue

        before = report.fields_changed
        outcome = process_record(cur, rec, report)
        delta = report.fields_changed - before

        cur.execute("UPDATE raw_source_rows SET candidate_id=%s WHERE id=%s",
                    (outcome.candidate_id, raw_id))

        cid = outcome.candidate_id
        if outcome.action == "create":
            created.add(cid)
        else:
            if cid not in created:
                preexisting_touched.add(cid)
                changes_by_cand[cid] = changes_by_cand.get(cid, 0) + delta
            if cid not in attach_rule and outcome.rule:
                attach_rule[cid] = outcome.rule

        if outcome.declined_existing:
            report.add_decline(
                sorted(x for x in [outcome.declined_existing_row, rec.source_row_id] if x),
                "name_match_only_insufficient",
                "same normalized_name, different strong identifiers",
            )

    # entity counters
    report.entities_created = len(created)
    report.entities_updated = sum(1 for c in preexisting_touched if changes_by_cand.get(c, 0) > 0)
    report.entities_unchanged_noop = sum(1 for c in preexisting_touched if changes_by_cand.get(c, 0) == 0)

    # merges: any touched candidate that >1 raw row resolves to
    touched = created | preexisting_touched
    for cid in sorted(touched):
        cur.execute(
            "SELECT source_row_id FROM raw_source_rows "
            "WHERE candidate_id=%s AND NOT rejected AND source_row_id IS NOT NULL "
            "ORDER BY source_row_id", (cid,))
        rows = [r[0] for r in cur.fetchall()]
        if len(rows) > 1:
            report.add_merge(cid, attach_rule.get(cid, "identifier_exact"), rows)

    # finalize derived state for everything we touched
    finalize_canonical_identities(cur, touched)
    finalize_metrics(cur, touched)

    report.enrichment["budget_calls"] = config.ENRICHMENT_BUDGET_CALLS

    # persist the report
    payload = report.to_dict()
    cur.execute(
        "INSERT INTO run_reports (run_id, batch, started_at, finished_at, report) "
        "VALUES (%s,%s,%s,%s,%s)",
        (ids.run_id(), batch, report.started_at, datetime.utcnow(),
         json.dumps(payload)),
    )
    conn.commit()

    os.makedirs(config.OUT_DIR, exist_ok=True)
    # submission expects run_report_batch01.json / batch02 (no middle underscore)
    out_path = os.path.join(config.OUT_DIR, f"run_report_{batch.replace('_', '')}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return payload


def main(argv):
    if len(argv) < 2:
        print("usage: python -m src.ingest <batch_name>  (e.g. batch_01)", file=sys.stderr)
        return 2
    batch = argv[1]
    path = os.path.join(config.DATA_DIR, f"{batch}_raw.jsonl")
    if not os.path.exists(path):
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    conn = db.connect()
    try:
        db.ensure_schema(conn)
        payload = ingest_batch(conn, batch, path)
    finally:
        conn.close()

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

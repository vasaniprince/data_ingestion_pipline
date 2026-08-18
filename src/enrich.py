"""Enrichment on a budget -- look up missing contact info, never overspend.

    python -m src.enrich

Two independent, idempotent passes (see DECISIONS.md sec 8 for why they're
split): BILLING (never re-billed once a key has a row in enrichment_cache)
and MERGING (retried every run until a 'found' row's fields have actually
been written onto its candidate, via enrichment_cache.merged_at). This means
a crash between billing and merging self-heals on the next run instead of
silently re-billing or silently losing the result.

Enrichment is just another source: found fields flow through the exact same
merge.apply_scalar precedence engine as linkedin/naukri/github (see Part 3).
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta

from . import config, db, ids
from . import identity
from . import merge
from .mapping import _email_identifier, _phone_identifier
from .report import RunReport

FAR_FUTURE = datetime(2099, 1, 1)


def _load_mock(path=None):
    path = path or config.ENRICHMENT_FILE
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.pop("_meta", None)
    return data


def _content_hash(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _insert_or_get_enrichment_raw(cur, cache_key, response, fetched_at):
    payload = {"_src": "enrichment", "_cache_key": cache_key, **response}
    ch = _content_hash(payload)
    cur.execute(
        "INSERT INTO raw_source_rows (batch, source, source_row_id, crawled_at, "
        " raw_payload, content_hash) VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (content_hash) DO NOTHING RETURNING id",
        ("enrichment", "enrichment", None, fetched_at, json.dumps(payload), ch),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT id FROM raw_source_rows WHERE content_hash=%s", (ch,))
    return cur.fetchone()[0]


def _skipped_no_key(cur):
    cur.execute(
        "SELECT candidate_id FROM candidates "
        "WHERE contactable=FALSE AND candidate_id NOT IN "
        "  (SELECT candidate_id FROM identities WHERE identity_type='linkedin_handle')"
    )
    return [r[0] for r in cur.fetchall()]


def _eligible_keys(cur):
    """candidate_id, cache_key, completeness_score for every contactable=false
    candidate that has a linkedin_handle identity (their own -- never guessed).
    Ranked by completeness_score DESC: a richer, more-observed profile is more
    confidently a real, employable candidate worth the spend (the brief's
    "who looks employable, who is worth spending on" selection rule)."""
    cur.execute(
        "SELECT c.candidate_id, i.match_key, c.completeness_score "
        "FROM candidates c JOIN identities i "
        "  ON i.candidate_id=c.candidate_id AND i.identity_type='linkedin_handle' "
        "WHERE c.contactable=FALSE "
        "ORDER BY c.completeness_score DESC, c.candidate_id ASC"
    )
    return [{"candidate_id": r[0], "cache_key": r[1], "completeness_score": float(r[2])}
            for r in cur.fetchall()]


def _billing_pool(cur, eligible):
    cur.execute("SELECT cache_key FROM enrichment_cache")
    already_cached = {r[0] for r in cur.fetchall()}
    pool = [e for e in eligible if e["cache_key"] not in already_cached]
    cached = [e for e in eligible if e["cache_key"] in already_cached]
    return pool, cached


def _bill(cur, cache_key, candidate_id, mock, fetched_at):
    if cache_key not in mock:
        return None
    resp = mock[cache_key]
    status = resp["status"]
    ttl = FAR_FUTURE if status == "found" else fetched_at + timedelta(
        days=config.ENRICHMENT_NEGATIVE_TTL_DAYS)
    cur.execute(
        "INSERT INTO enrichment_cache "
        "(cache_key, status, response, candidate_id, cost_inr, fetched_at, "
        " ttl_expires_at, merged_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,NULL)",
        (cache_key, status, json.dumps(resp), candidate_id,
         config.ENRICHMENT_COST_INR, fetched_at, ttl),
    )
    return {"status": status, "response": resp, "cost_inr": config.ENRICHMENT_COST_INR}


def _merge_found_row(cur, cache_key, candidate_id, response, fetched_at, report):
    conf = response.get("confidence") or config.SOURCE_CONFIDENCE["enrichment"]
    source_row_pk = _insert_or_get_enrichment_raw(cur, cache_key, response, fetched_at)

    for email_field in ("work_email", "personal_email"):
        raw = response.get(email_field)
        if raw:
            ident = _email_identifier(raw, conf)
            if ident:
                identity._upsert_identity(cur, candidate_id, ident, "enrichment", fetched_at)
    mobile = response.get("mobile")
    if mobile:
        ident = _phone_identifier(mobile, conf)
        if ident:
            identity._upsert_identity(cur, candidate_id, ident, "enrichment", fetched_at)

    for field, key in (("current_title", "current_title"), ("current_company", "current_company")):
        value = response.get(key)
        if value is not None:
            merge.apply_scalar(cur, candidate_id, field, value, "enrichment",
                               source_row_pk, fetched_at, conf, "enrichment", report)

    cur.execute("UPDATE enrichment_cache SET merged_at=%s WHERE cache_key=%s",
                (fetched_at, cache_key))


def run_enrichment(conn):
    cur = conn.cursor()
    report = RunReport("enrichment")
    now = datetime.utcnow()

    cur.execute("SELECT count(*) FROM candidates WHERE contactable")
    contactable_before = cur.fetchone()[0]

    skipped_no_key = _skipped_no_key(cur)
    eligible = _eligible_keys(cur)
    pool, already_cached = _billing_pool(cur, eligible)

    cur.execute("SELECT count(*) FROM enrichment_cache")
    remaining_budget = config.ENRICHMENT_BUDGET_CALLS - cur.fetchone()[0]
    remaining_budget = max(remaining_budget, 0)
    selected = pool[:remaining_budget]
    skipped_budget = pool[remaining_budget:]

    mock = _load_mock()
    calls_made = 0
    spend_inr = 0.0
    for item in selected:
        billed = _bill(cur, item["cache_key"], item["candidate_id"], mock, now)
        if billed:
            calls_made += 1
            spend_inr += billed["cost_inr"]

    # cache-hit accounting for the report: keys that were already cached
    # before this run's billing step (this is what "second run costs 0" shows)
    calls_served_from_cache = 0
    negative_cache_hits = 0
    saved_inr = 0.0
    for item in already_cached:
        cur.execute("SELECT status FROM enrichment_cache WHERE cache_key=%s",
                    (item["cache_key"],))
        row = cur.fetchone()
        if row is None:
            continue
        calls_served_from_cache += 1
        saved_inr += config.ENRICHMENT_COST_INR
        if row[0] == "not_found":
            negative_cache_hits += 1

    # catch-up merge pass -- runs every invocation, unconditionally
    cur.execute(
        "SELECT cache_key, candidate_id, response, fetched_at FROM enrichment_cache "
        "WHERE status='found' AND merged_at IS NULL AND candidate_id IS NOT NULL"
    )
    unmerged = cur.fetchall()
    touched = set()
    for cache_key, candidate_id, response, fetched_at in unmerged:
        _merge_found_row(cur, cache_key, candidate_id, response, fetched_at, report)
        touched.add(candidate_id)

    if touched:
        identity.finalize_canonical_identities(cur, touched)
        identity.finalize_metrics(cur, touched)

    cur.execute("SELECT count(*) FROM candidates WHERE contactable")
    contactable_after = cur.fetchone()[0]

    report.enrichment.update({
        "budget_calls": config.ENRICHMENT_BUDGET_CALLS,
        "calls_made": calls_made,
        "calls_served_from_cache": calls_served_from_cache,
        "negative_cache_hits": negative_cache_hits,
        "spend_inr": round(spend_inr, 2),
        "saved_inr": round(saved_inr, 2),
        "contactable_before": contactable_before,
        "contactable_after": contactable_after,
    })
    report.rows_in = len(eligible) + len(skipped_no_key)

    payload = report.to_dict()
    payload["skipped_no_key"] = skipped_no_key
    payload["skipped_budget"] = [x["candidate_id"] for x in skipped_budget]
    payload["selected"] = [x["candidate_id"] for x in selected]

    cur.execute(
        "INSERT INTO run_reports (run_id, batch, started_at, finished_at, report) "
        "VALUES (%s,%s,%s,%s,%s)",
        (ids.run_id(), "enrichment", report.started_at, datetime.utcnow(),
         json.dumps(payload)),
    )
    conn.commit()

    os.makedirs(config.OUT_DIR, exist_ok=True)
    out_path = os.path.join(config.OUT_DIR, "run_report_enrichment.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return payload


def main(argv):
    conn = db.connect()
    try:
        db.ensure_schema(conn)
        payload = run_enrichment(conn)
    finally:
        conn.close()
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

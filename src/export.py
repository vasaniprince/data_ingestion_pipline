"""The actual deliverable: out/candidates_enriched.csv.

    python -m src.export

Reads only -- every metric it writes (completeness_score, contactable,
source_count, identity_count, primary_email/phone via is_canonical) is already
maintained by identity.finalize_metrics / finalize_canonical_identities. This
module recomputes nothing; it just flattens the stored state into the exact
22-column shape from Appendix A.3. Deterministic (ORDER BY candidate_id, which
sorts chronologically since candidate_id is a ULID) so re-running against an
unchanged table produces a byte-identical file -- the CSV half of "run it
twice and nothing changes."
"""
import csv
import os
import sys

from . import config, db

COLUMNS = [
    "candidate_id", "full_name", "current_title", "current_company", "city",
    "country", "experience_months", "annual_salary_inr", "notice_period_days",
    "skills", "open_to_work", "primary_email", "primary_phone", "contactable",
    "completeness_score", "source_count", "identity_count", "first_seen_at",
    "last_seen_at", "enriched", "enrichment_cost_inr", "data_quality_flags",
]

_QUERY = """
SELECT
    c.candidate_id, c.full_name, c.current_title, c.current_company,
    c.location_city, c.location_country, c.experience_months,
    c.annual_salary_inr, c.notice_period_days, c.skills, c.open_to_work,
    (SELECT i.value FROM identities i WHERE i.candidate_id=c.candidate_id
        AND i.identity_type='email' AND i.is_canonical LIMIT 1) AS primary_email,
    (SELECT i.value FROM identities i WHERE i.candidate_id=c.candidate_id
        AND i.identity_type='phone' AND i.is_canonical LIMIT 1) AS primary_phone,
    c.contactable, c.completeness_score, c.source_count, c.identity_count,
    c.first_seen_at, c.last_seen_at,
    EXISTS (SELECT 1 FROM enrichment_cache ec WHERE ec.candidate_id=c.candidate_id
        AND ec.status='found' AND ec.merged_at IS NOT NULL) AS enriched,
    COALESCE((SELECT SUM(ec.cost_inr) FROM enrichment_cache ec
        WHERE ec.candidate_id=c.candidate_id), 0) AS enrichment_cost_inr,
    c.data_quality_flags
FROM candidates c
ORDER BY c.candidate_id
"""


def _bool_str(value):
    # open_to_work is nullable; the brief's own CSV example only ever shows
    # true/false, never blank, so an unobserved signal renders as false --
    # not a claim the person isn't open, just the absence of a stronger one.
    return "true" if value else "false"


def _row_to_csv(row):
    (candidate_id, full_name, title, company, city, country, exp_months,
     salary, notice, skills, open_to_work, primary_email, primary_phone,
     contactable, completeness_score, source_count, identity_count,
     first_seen_at, last_seen_at, enriched, enrichment_cost_inr,
     flags) = row
    return [
        candidate_id,
        full_name or "",
        title or "",
        company or "",
        city or "",
        country or "",
        exp_months if exp_months is not None else "",
        salary if salary is not None else "",
        notice if notice is not None else "",
        ";".join(skills or []),
        _bool_str(open_to_work),
        primary_email or "",
        primary_phone or "",
        _bool_str(contactable),
        f"{float(completeness_score):.3f}",
        source_count,
        identity_count,
        first_seen_at.isoformat() if first_seen_at else "",
        last_seen_at.isoformat() if last_seen_at else "",
        _bool_str(enriched),
        f"{float(enrichment_cost_inr):.2f}",
        ";".join(flags or []),
    ]


def export_csv(conn, out_path=None):
    out_path = out_path or os.path.join(config.OUT_DIR, "candidates_enriched.csv")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    with conn.cursor() as cur:
        cur.execute(_QUERY)
        rows = cur.fetchall()

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow(_row_to_csv(row))

    return {"path": out_path, "rows_written": len(rows)}


def main(argv):
    conn = db.connect()
    try:
        db.ensure_schema(conn)
        result = export_csv(conn)
    finally:
        conn.close()
    print(f"wrote {result['rows_written']} rows to {result['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

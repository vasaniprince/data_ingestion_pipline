"""A small FastAPI service over the candidate table.

    uvicorn src.api:app --host 0.0.0.0 --port 8000

Three endpoints, read-only, raw SQL (no ORM, consistent with the rest of this
project). Every metric returned (completeness_score, contactable, primary
email/phone, etc.) is read straight off the precomputed columns/is_canonical
flags maintained by identity.finalize_metrics / finalize_canonical_identities
-- this module never recomputes them.
"""
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import config, db

app = FastAPI(title="SARAL candidate service")


@app.on_event("startup")
def _startup():
    conn = db.connect()
    try:
        db.ensure_schema(conn)
    finally:
        conn.close()


def _conn():
    return db.connect(config.DB_DSN)


def _candidate_summary_row(row):
    (candidate_id, full_name, title, company, city, state, country,
     exp_months, salary, notice, skills, open_to_work, contactable,
     completeness_score, source_count, identity_count, first_seen_at,
     last_seen_at, flags) = row
    return {
        "candidate_id": candidate_id,
        "full_name": full_name,
        "current_title": title,
        "current_company": company,
        "location": {"city": city, "state": state, "country": country},
        "experience_months": exp_months,
        "annual_salary_inr": salary,
        "notice_period_days": notice,
        "skills": skills or [],
        "open_to_work": open_to_work,
        "contactable": contactable,
        "completeness_score": float(completeness_score) if completeness_score is not None else None,
        "source_count": source_count,
        "identity_count": identity_count,
        "first_seen_at": first_seen_at.isoformat() if first_seen_at else None,
        "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
        "data_quality_flags": flags or [],
    }


_SEARCH_COLUMNS = """
    candidate_id, full_name, current_title, current_company, location_city,
    location_state, location_country, experience_months, annual_salary_inr,
    notice_period_days, skills, open_to_work, contactable, completeness_score,
    source_count, identity_count, first_seen_at, last_seen_at, data_quality_flags
"""


@app.get("/candidates")
def search_candidates(
    q: str | None = None,
    role: str | None = None,
    location: str | None = None,
    open_to_work: bool | None = None,
    min_experience: float | None = Query(None, description="years"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    where = []
    params = []

    if q:
        where.append("(normalized_name ILIKE %s OR current_title ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    if role:
        where.append("(current_title ILIKE %s OR %s = ANY(skills))")
        params.extend([f"%{role}%", role.lower()])
    if location:
        where.append("location_city ILIKE %s")
        params.append(f"%{location}%")
    if open_to_work is not None:
        where.append("open_to_work = %s")
        params.append(open_to_work)
    if min_experience is not None:
        where.append("experience_months >= %s")
        params.append(min_experience * 12)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM candidates {where_sql}", params)
            total = cur.fetchone()[0]

            cur.execute(
                f"SELECT {_SEARCH_COLUMNS} FROM candidates {where_sql} "
                "ORDER BY completeness_score DESC, candidate_id ASC "
                "LIMIT %s OFFSET %s",
                params + [limit, offset],
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [_candidate_summary_row(r) for r in rows],
    }


@app.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT candidate_id, full_name, normalized_name, current_title, "
                " current_company, location_raw, location_city, location_state, "
                " location_country, experience_months, annual_salary_inr, "
                " notice_period_days, skills, open_to_work, contactable, "
                " completeness_score, source_count, identity_count, "
                " first_seen_at, last_seen_at, data_quality_flags "
                "FROM candidates WHERE candidate_id=%s", (candidate_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="candidate not found")

            (cid, full_name, normalized_name, title, company, loc_raw, city,
             state, country, exp_months, salary, notice, skills, open_to_work,
             contactable, completeness_score, source_count, identity_count,
             first_seen_at, last_seen_at, flags) = row

            cur.execute(
                "SELECT identity_type, value, match_key, raw_forms, observed_in, "
                " email_type, confidence, is_canonical, first_seen_at "
                "FROM identities WHERE candidate_id=%s "
                "ORDER BY identity_type, is_canonical DESC", (candidate_id,))
            identities = []
            emails, phones = [], []
            for (itype, value, match_key, raw_forms, observed_in, email_type,
                 confidence, is_canonical, ident_first_seen) in cur.fetchall():
                identities.append({
                    "type": itype, "value": value, "canonical": is_canonical,
                    "observed_in": observed_in or [], "raw_forms": raw_forms or [],
                    "first_seen": ident_first_seen.isoformat() if ident_first_seen else None,
                })
                conf = float(confidence) if confidence is not None else None
                source = (observed_in or [None])[-1]
                if itype == "email":
                    emails.append({"value": value, "type": email_type,
                                    "source": source, "confidence": conf})
                elif itype == "phone":
                    phones.append({"value": value, "source": source, "confidence": conf})

            cur.execute(
                "SELECT fp.field_name, fp.value, fp.source, r.source_row_id, "
                " fp.observed_at, fp.confidence, fp.note, fp.is_current, fp.reason "
                "FROM field_provenance fp "
                "LEFT JOIN raw_source_rows r ON r.id = fp.source_row_id "
                "WHERE fp.candidate_id=%s "
                "ORDER BY fp.field_name, fp.observed_at DESC", (candidate_id,))
            field_provenance = {}
            for (field_name, value, source, source_row, observed_at, confidence,
                 note, is_current, reason) in cur.fetchall():
                entry = {
                    "value": value, "source": source, "source_row": source_row,
                    "observed_at": observed_at.isoformat() if observed_at else None,
                    "confidence": float(confidence) if confidence is not None else None,
                    "note": note,
                }
                if is_current:
                    entry["superseded"] = []
                    field_provenance[field_name] = entry
                elif field_name in field_provenance:
                    field_provenance[field_name]["superseded"].append({
                        "value": value, "source": source,
                        "observed_at": entry["observed_at"], "reason": reason,
                    })

            cur.execute(
                "SELECT cl.event_id, cl.field_name, cl.old_value, cl.new_value, "
                " cl.source, r.source_row_id, cl.batch, cl.applied_at, cl.rule, cl.note "
                "FROM change_log cl "
                "LEFT JOIN raw_source_rows r ON r.id = cl.source_row_id "
                "WHERE cl.candidate_id=%s "
                "ORDER BY cl.applied_at DESC NULLS LAST", (candidate_id,))
            change_log = [
                {
                    "event_id": event_id, "field": field_name,
                    "old_value": old_value, "new_value": new_value,
                    "source": source, "source_row": source_row, "batch": batch,
                    "applied_at": applied_at.isoformat() if applied_at else None,
                    "rule": rule, "note": note,
                }
                for (event_id, field_name, old_value, new_value, source,
                     source_row, batch, applied_at, rule, note) in cur.fetchall()
            ]
    finally:
        conn.close()

    return {
        "candidate_id": cid,
        "full_name": full_name,
        "normalized_name": normalized_name,
        "current_title": title,
        "current_company": company,
        "location": {"raw": loc_raw, "city": city, "state": state, "country": country},
        "experience_months": exp_months,
        "annual_salary_inr": salary,
        "notice_period_days": notice,
        "skills": skills or [],
        "open_to_work": open_to_work,
        "emails": emails,
        "phones": phones,
        "contactable": contactable,
        "completeness_score": float(completeness_score) if completeness_score is not None else None,
        "source_count": source_count,
        "identity_count": identity_count,
        "first_seen_at": first_seen_at.isoformat() if first_seen_at else None,
        "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
        "data_quality_flags": flags or [],
        "identities": identities,
        "field_provenance": field_provenance,
        "change_log": change_log,
    }


@app.get("/stats")
def stats(batch: str | None = None):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            if batch:
                cur.execute(
                    "SELECT run_id, batch, started_at, finished_at, report "
                    "FROM run_reports WHERE batch=%s "
                    "ORDER BY started_at DESC LIMIT 1", (batch,))
            else:
                cur.execute(
                    "SELECT run_id, batch, started_at, finished_at, report "
                    "FROM run_reports ORDER BY started_at DESC LIMIT 1")
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="no run reports yet")

    run_id, run_batch, started_at, finished_at, report = row
    return JSONResponse({
        "run_id": run_id,
        "batch": run_batch,
        "started_at": started_at.isoformat() if started_at else None,
        "finished_at": finished_at.isoformat() if finished_at else None,
        "report": report,
    })

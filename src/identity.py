"""Identity resolution + candidate create/attach/entity-merge.

Resolution rules, ranked by strength (see DECISIONS.md):
  STRONG, conclusive alone (DB-enforced via UNIQUE(identity_type, match_key)):
    1. linkedin_handle_exact
    2. email_exact
    3. phone_exact
    4. github_login_exact
  COMBINATION, only consulted when NO strong identifier matched anything:
    5. name_company_city_title -- normalized_name AND company-key AND city
       AND current_title ALL match a single existing candidate. Three
       independent weak signals agreeing is the "evidence in combination"
       the brief asks for (an earlier two-signal version -- name+company+city
       -- was reviewed and rejected as too weak; see DECISIONS.md §2.2/§7 and
       WRITEUP.md); it only fires when the record has company, city, AND
       title to check, and only when exactly one candidate matches
       (ambiguity -> no match). Matches are flagged combination_match_applied
       for audit.
  WEAK, NEVER merges alone: normalized_name. A same-name row with no shared
  strong identifier AND no company+city combination match is DECLINED
  (separate candidate + name_collision_reviewed flag), because a false merge
  is unrecoverable and graded as the worse error. This is why the two Aaravs
  (different company AND city) never merge, while Vikram (same company AND
  city) does.
"""
import json
from dataclasses import dataclass
from datetime import datetime

from . import ids, merge
from . import normalize as n
from .config import (FIELD_SOURCE_TRUST, DEFAULT_TRUST_RANK, MERGE_SCALAR_FIELDS)

# strength order for choosing which rule to report when several identifiers match
_RULE_STRENGTH = ["linkedin_handle", "email", "phone", "github_login"]


@dataclass
class Outcome:
    action: str                 # 'create' | 'attach' | 'entity_merge'
    candidate_id: str
    rule: str | None = None     # identifier rule that linked rows (attach/merge)
    declined_existing: str | None = None      # candidate_id of a same-name collision
    declined_existing_row: int | None = None


def _parse_ts(ts):
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# -- lookups ---------------------------------------------------------------

def _match_candidates(cur, rec):
    """Return (matches, best_rule) where matches maps candidate_id -> set of
    rules that pointed at it, and best_rule is the strongest matching rule."""
    matches = {}
    rules_hit = []
    for ident in rec.identifiers:
        cur.execute(
            "SELECT candidate_id FROM identities WHERE identity_type=%s AND match_key=%s",
            (ident.itype, ident.match_key),
        )
        row = cur.fetchone()
        if row:
            matches.setdefault(row[0], set()).add(ident.itype)
            rules_hit.append(ident.itype)
    best_rule = None
    for t in _RULE_STRENGTH:
        if t in rules_hit:
            best_rule = t + "_exact"
            break
    return matches, best_rule


def _combination_match(cur, rec):
    """name + company + location + title combination rule. Only called when
    the strong-identifier lookup found nothing. Returns a candidate_id or None.

    THREE independent signals are required (company, city, AND title), not
    two -- two different people sharing a name and even an employer+city is
    plausible at scale; requiring title as well makes that coincidence much
    less likely while still catching the real case in this dataset (Vikram
    Singh's naukri row and his existing record agree on all three). Still
    conservative in the same way as before: requires all of company/city/title
    non-null on the incoming record, and requires the match to be UNIQUE among
    existing candidates sharing that name -- an ambiguous match is refused
    rather than guessed at. See DECISIONS.md for the residual risk that
    remains even with three signals.
    """
    if not rec.normalized_name:
        return None
    company_key = n.normalize_company_key(rec.fields.get("current_company"))
    loc = rec.fields.get("location") or {}
    city = loc.get("city")
    title = n.clean_str(rec.fields.get("current_title"))
    title_key = title.lower() if title else None
    if not company_key or not city or not title_key:
        return None

    cur.execute(
        "SELECT candidate_id, current_company, location_city, current_title "
        "FROM candidates WHERE normalized_name=%s", (rec.normalized_name,),
    )
    hits = [
        row[0] for row in cur.fetchall()
        if n.normalize_company_key(row[1]) == company_key
        and row[2] == city
        and row[3] and row[3].lower() == title_key
    ]
    if len(hits) == 1:
        return hits[0]
    return None  # zero or ambiguous (>1) -> no match


def _same_name_candidate(cur, rec, exclude=None):
    if not rec.normalized_name:
        return None, None
    cur.execute(
        "SELECT candidate_id FROM candidates WHERE normalized_name=%s "
        "AND candidate_id <> %s LIMIT 1",
        (rec.normalized_name, exclude or ""),
    )
    row = cur.fetchone()
    if not row:
        return None, None
    cid = row[0]
    cur.execute(
        "SELECT min(source_row_id) FROM raw_source_rows WHERE candidate_id=%s", (cid,))
    rep = cur.fetchone()[0]
    return cid, rep


def _add_flag(cur, candidate_id, flag):
    """Append `flag` to a candidate's data_quality_flags, deduped. Used to
    make resolver decisions (name collisions, combination merges) visible on
    the record itself, not just in DECISIONS.md."""
    cur.execute(
        "UPDATE candidates SET data_quality_flags = "
        "(SELECT array_agg(DISTINCT e) FROM unnest(data_quality_flags || "
        "ARRAY[%s]) e) WHERE candidate_id=%s",
        (flag, candidate_id),
    )


# -- identity writes -------------------------------------------------------

def _upsert_identity(cur, candidate_id, ident, source, first_seen_at):
    cur.execute(
        "INSERT INTO identities (candidate_id, identity_type, value, match_key, "
        " raw_forms, observed_in, email_type, confidence, first_seen_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (identity_type, match_key) DO UPDATE SET "
        "  raw_forms = (SELECT array_agg(DISTINCT e) FROM unnest("
        "     identities.raw_forms || EXCLUDED.raw_forms) e), "
        "  observed_in = (SELECT array_agg(DISTINCT e) FROM unnest("
        "     identities.observed_in || EXCLUDED.observed_in) e), "
        "  confidence = GREATEST(identities.confidence, EXCLUDED.confidence)",
        (candidate_id, ident.itype, ident.value, ident.match_key,
         [ident.raw_form] if ident.raw_form else [], [source],
         ident.email_type, ident.confidence, first_seen_at),
    )


# -- creation --------------------------------------------------------------

def _create_candidate(cur, rec, report):
    cid = ids.candidate_id()
    obs_at = rec.crawled_at
    flags = []

    # name-collision decline check (only meaningful if we have a name)
    decl_cid, decl_row = _same_name_candidate(cur, rec)
    if decl_cid is not None:
        flags.append("name_collision_reviewed")
        _add_flag(cur, decl_cid, "name_collision_reviewed")

    loc = rec.fields.get("location") or {}
    cur.execute(
        "INSERT INTO candidates (candidate_id, full_name, normalized_name, "
        " current_title, current_company, location_raw, location_city, "
        " location_state, location_country, experience_months, annual_salary_inr, "
        " notice_period_days, open_to_work, skills, data_quality_flags, "
        " first_seen_at, last_seen_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (cid, rec.display_name, rec.normalized_name,
         rec.fields.get("current_title"), rec.fields.get("current_company"),
         loc.get("raw"), loc.get("city"), loc.get("state"), loc.get("country"),
         rec.fields.get("experience_months"), rec.fields.get("annual_salary_inr"),
         rec.fields.get("notice_period_days"), rec.fields.get("open_to_work"),
         sorted(set(rec.skills)), flags, obs_at, obs_at),
    )

    # provenance for each present, non-null scalar field (creation: NO change_log)
    for f in MERGE_SCALAR_FIELDS:
        if f in rec.fields and rec.fields[f] is not None:
            cur.execute(
                "INSERT INTO field_provenance (candidate_id, field_name, value, "
                " source, source_row_id, observed_at, confidence, is_current) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE)",
                (cid, f, json.dumps(rec.fields[f]), rec.source, rec.source_row_pk,
                 obs_at, rec.confidence),
            )

    _write_identities(cur, cid, rec)
    return Outcome("create", cid, declined_existing=decl_cid, declined_existing_row=decl_row)


def _write_identities(cur, candidate_id, rec):
    for ident in rec.identifiers:
        _upsert_identity(cur, candidate_id, ident, rec.source, rec.crawled_at)


# -- attach ----------------------------------------------------------------

def _attach(cur, candidate_id, rec, report, rule):
    # out-of-order detection: does this row describe a time earlier than what
    # we already have for this candidate?
    cur.execute("SELECT last_seen_at, first_seen_at FROM candidates WHERE candidate_id=%s",
                (candidate_id,))
    last_seen, first_seen = cur.fetchone()
    inc_ts = _parse_ts(rec.crawled_at)
    if inc_ts and last_seen and inc_ts < last_seen:
        report.out_of_order_records += 1

    _write_identities(cur, candidate_id, rec)

    for f in MERGE_SCALAR_FIELDS:
        if f in rec.fields:
            merge.apply_scalar(cur, candidate_id, f, rec.fields[f], rec.source,
                               rec.source_row_pk, rec.crawled_at, rec.confidence,
                               rec.batch, report)
    merge.apply_skills_union(cur, candidate_id, rec.skills, rec.source,
                             rec.source_row_pk, rec.crawled_at, rec.batch, report)

    # widen the seen-window from data (never now())
    cur.execute(
        "UPDATE candidates SET last_seen_at=GREATEST(last_seen_at, %s), "
        "first_seen_at=LEAST(first_seen_at, %s) WHERE candidate_id=%s",
        (rec.crawled_at, rec.crawled_at, candidate_id),
    )
    return Outcome("attach", candidate_id, rule=rule)


# -- entity merge (two existing candidates are one person) -----------------

def _entity_merge(cur, candidate_ids, rec):
    """Repoint everything from losers to the survivor, then delete losers.
    Survivor = MIN(first_seen_at) then MIN(candidate_id). One transaction."""
    cur.execute(
        "SELECT candidate_id FROM candidates WHERE candidate_id = ANY(%s) "
        "ORDER BY first_seen_at ASC, candidate_id ASC",
        (list(candidate_ids),),
    )
    ordered = [r[0] for r in cur.fetchall()]
    survivor, losers = ordered[0], ordered[1:]
    for loser in losers:
        for tbl in ("identities", "raw_source_rows", "field_provenance",
                    "change_log", "enrichment_cache"):
            cur.execute(f"UPDATE {tbl} SET candidate_id=%s WHERE candidate_id=%s",
                        (survivor, loser))
        cur.execute(
            "UPDATE candidates SET "
            " first_seen_at=LEAST((SELECT first_seen_at FROM candidates WHERE candidate_id=%s), "
            "   (SELECT first_seen_at FROM candidates WHERE candidate_id=%s)), "
            " last_seen_at=GREATEST((SELECT last_seen_at FROM candidates WHERE candidate_id=%s), "
            "   (SELECT last_seen_at FROM candidates WHERE candidate_id=%s)) "
            "WHERE candidate_id=%s",
            (survivor, loser, survivor, loser, survivor),
        )
        cur.execute("DELETE FROM candidates WHERE candidate_id=%s", (loser,))
    return survivor


# -- public entry ----------------------------------------------------------

def process_record(cur, rec, report):
    """Resolve + upsert one record. Caller has already inserted the raw row and
    set rec.source_row_pk, and has already rejected id-less rows."""
    matches, best_rule = _match_candidates(cur, rec)

    if not matches:
        combo_cid = _combination_match(cur, rec)
        if combo_cid is not None:
            _add_flag(cur, combo_cid, "combination_match_applied")
            return _attach(cur, combo_cid, rec, report, "name_company_location")
        return _create_candidate(cur, rec, report)

    if len(matches) == 1:
        cid = next(iter(matches))
        return _attach(cur, cid, rec, report, best_rule)

    # >1 existing candidate -> they are one person
    survivor = _entity_merge(cur, set(matches.keys()), rec)
    out = _attach(cur, survivor, rec, report, best_rule or "entity_merge")
    out.action = "entity_merge"
    return out


# -- finalize passes -------------------------------------------------------

def finalize_canonical_identities(cur, candidate_ids):
    """Pick one is_canonical identifier per (candidate, type): emails prefer
    work then higher confidence; others prefer higher confidence."""
    for cid in candidate_ids:
        cur.execute(
            "SELECT id, identity_type, email_type, confidence FROM identities "
            "WHERE candidate_id=%s", (cid,))
        rows = cur.fetchall()
        by_type = {}
        for _id, itype, etype, conf in rows:
            by_type.setdefault(itype, []).append((_id, etype, float(conf or 0)))
        cur.execute("UPDATE identities SET is_canonical=FALSE WHERE candidate_id=%s", (cid,))
        for itype, group in by_type.items():
            if itype == "email":
                group.sort(key=lambda x: (0 if x[1] == "work" else 1, -x[2]))
            else:
                group.sort(key=lambda x: -x[2])
            cur.execute("UPDATE identities SET is_canonical=TRUE WHERE id=%s", (group[0][0],))


WANTED_SIGNALS = 11  # denominator for completeness_score (see DECISIONS.md)


def finalize_metrics(cur, candidate_ids):
    for cid in candidate_ids:
        cur.execute("SELECT source FROM raw_source_rows WHERE candidate_id=%s", (cid,))
        source_count = len({r[0] for r in cur.fetchall()})
        cur.execute("SELECT identity_type FROM identities WHERE candidate_id=%s", (cid,))
        itypes = [r[0] for r in cur.fetchall()]
        identity_count = len(itypes)
        contactable = ("email" in itypes) or ("phone" in itypes)

        cur.execute(
            "SELECT full_name, current_title, current_company, location_city, "
            " experience_months, annual_salary_inr, notice_period_days, "
            " open_to_work, skills FROM candidates WHERE candidate_id=%s", (cid,))
        (full_name, title, company, city, exp, salary, notice, otw, skills) = cur.fetchone()
        signals = [
            full_name, title, company, city, exp, salary, notice,
            otw is not None, bool(skills), ("email" in itypes), ("phone" in itypes),
        ]
        filled = sum(1 for s in signals if s)
        score = round(filled / WANTED_SIGNALS, 3)

        cur.execute(
            "UPDATE candidates SET source_count=%s, identity_count=%s, "
            "contactable=%s, completeness_score=%s WHERE candidate_id=%s",
            (source_count, identity_count, contactable, score, cid),
        )

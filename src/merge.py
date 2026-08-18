"""Field-level merge engine.

Given an existing candidate and one incoming observation of a field, decide
whether to replace / fill / ignore / no-op, and write field_provenance +
change_log accordingly. The cardinal rules (see DECISIONS.md):

  * non-null beats null  -> a source's null NEVER deletes a populated field
  * source-trust > recency > confidence  -> deterministic, not last-write-wins
  * an identical value writes NOTHING     -> zero change rows on a no-op re-crawl
  * skills is a growing UNION, never shrinks

All writes go through the passed cursor; the caller owns the transaction.
"""
import json
from datetime import datetime

from . import ids
from .config import FIELD_SOURCE_TRUST, DEFAULT_TRUST_RANK, NULL_PROTECTED_FIELDS


def _parse_ts(ts):
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _trust(field, source):
    return FIELD_SOURCE_TRUST.get(field, {}).get(source, DEFAULT_TRUST_RANK)


def _values_equal(field, a, b):
    """Value equality for change detection. `location` compares only the
    structured (city, state, country) components -- two different raw
    spellings of the same place ("Pune, MH" vs "Pune, Maharashtra, India")
    are the same fact and must not churn the change log."""
    if field == "location":
        a, b = (a or {}), (b or {})
        key = lambda d: (d.get("city"), d.get("state"), d.get("country"))
        return key(a) == key(b)
    return a == b


def _decide(field, inc, cur):
    """Both values non-null and different. Returns (winner, rule).
    winner in {'incoming','current'}."""
    ir, cr = _trust(field, inc["source"]), _trust(field, cur["source"])
    if ir < cr:
        return "incoming", "source_trust"
    if ir > cr:
        return "current", "source_trust"
    ia, ca = _parse_ts(inc["observed_at"]), _parse_ts(cur["observed_at"])
    if ia and ca and ia > ca:
        return "incoming", ("newer_observation_same_source"
                            if inc["source"] == cur["source"] else "recency")
    if ia and ca and ia < ca:
        return "current", "recency"
    if (inc["confidence"] or 0) > (cur["confidence"] or 0):
        return "incoming", "confidence"
    return "current", "incumbent_kept"


# candidate-column writers -------------------------------------------------

def _write_candidate_column(cur, candidate_id, field, value):
    if field == "location":
        loc = value or {}
        cur.execute(
            "UPDATE candidates SET location_raw=%s, location_city=%s, "
            "location_state=%s, location_country=%s, updated_at=now() "
            "WHERE candidate_id=%s",
            (loc.get("raw"), loc.get("city"), loc.get("state"),
             loc.get("country"), candidate_id),
        )
    elif field == "full_name":
        norm = value.lower() if value else None
        cur.execute(
            "UPDATE candidates SET full_name=%s, normalized_name=%s, updated_at=now() "
            "WHERE candidate_id=%s",
            (value, norm, candidate_id),
        )
    else:
        cur.execute(
            f"UPDATE candidates SET {field}=%s, updated_at=now() WHERE candidate_id=%s",
            (value, candidate_id),
        )


def _current_provenance(cur, candidate_id, field):
    cur.execute(
        "SELECT value, source, source_row_id, observed_at, confidence "
        "FROM field_provenance WHERE candidate_id=%s AND field_name=%s AND is_current",
        (candidate_id, field),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"value": row[0], "source": row[1], "source_row_id": row[2],
            "observed_at": row[3], "confidence": float(row[4]) if row[4] is not None else None}


def _insert_provenance(cur, candidate_id, field, obs, note=None):
    cur.execute(
        "INSERT INTO field_provenance "
        "(candidate_id, field_name, value, source, source_row_id, observed_at, "
        " confidence, note, is_current) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE)",
        (candidate_id, field, json.dumps(obs["value"]), obs["source"],
         obs["source_row_id"], obs["observed_at"], obs["confidence"], note),
    )


def _supersede_current(cur, candidate_id, field, reason):
    cur.execute(
        "UPDATE field_provenance SET is_current=FALSE, reason=%s "
        "WHERE candidate_id=%s AND field_name=%s AND is_current",
        (reason, candidate_id, field),
    )


def _log_change(cur, candidate_id, field, old_value, new_value, source,
                source_row_id, batch, rule, note=None, applied=True):
    cur.execute(
        "INSERT INTO change_log "
        "(event_id, candidate_id, field_name, old_value, new_value, source, "
        " source_row_id, batch, rule, note, applied_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, %s) "
        "ON CONFLICT (candidate_id, field_name, source_row_id, rule) DO NOTHING",
        (ids.change_id(), candidate_id, field, json.dumps(old_value),
         json.dumps(new_value), source, source_row_id, batch, rule, note,
         datetime.utcnow() if applied else None),
    )


# public API ----------------------------------------------------------------

def apply_scalar(cur, candidate_id, field, value, source, source_row_id,
                 observed_at, confidence, batch, report, note=None):
    """Merge one scalar field observation into an existing candidate."""
    inc = {"value": value, "source": source, "source_row_id": source_row_id,
           "observed_at": observed_at, "confidence": confidence}
    cur_prov = _current_provenance(cur, candidate_id, field)

    # incoming empty
    if value is None:
        if cur_prov is not None and cur_prov["value"] is not None:
            if field in NULL_PROTECTED_FIELDS:
                _log_change(cur, candidate_id, field, cur_prov["value"], None,
                            source, source_row_id, batch,
                            "REJECTED:null_from_source_is_not_deletion",
                            note="incoming null; existing non-null retained",
                            applied=False)
                report.nulls_ignored += 1
        return

    # field never populated -> fill
    if cur_prov is None or cur_prov["value"] is None:
        _insert_provenance(cur, candidate_id, field, inc, note)
        _write_candidate_column(cur, candidate_id, field, value)
        _log_change(cur, candidate_id, field, None, value, source,
                    source_row_id, batch, "fill_value", note)
        report.fields_changed += 1
        return

    # identical value (or, for location, identical city/state/country) -> no-op
    if _values_equal(field, cur_prov["value"], value):
        return

    # genuine conflict
    winner, rule = _decide(field, inc, cur_prov)
    report.add_conflict(field,
                        winner=source if winner == "incoming" else cur_prov["source"],
                        loser=cur_prov["source"] if winner == "incoming" else source,
                        rule=rule)
    if winner == "incoming":
        _supersede_current(cur, candidate_id, field, rule)
        _insert_provenance(cur, candidate_id, field, inc, note)
        _write_candidate_column(cur, candidate_id, field, value)
        _log_change(cur, candidate_id, field, cur_prov["value"], value, source,
                    source_row_id, batch, rule, note)
        report.fields_changed += 1
    # else: incumbent kept -> nothing written, only counted as a conflict


def apply_skills_union(cur, candidate_id, incoming_skills, source, source_row_id,
                       observed_at, batch, report):
    """skills is a growing union. Adds new skills only; never removes."""
    if not incoming_skills:
        return
    cur.execute("SELECT skills FROM candidates WHERE candidate_id=%s", (candidate_id,))
    current = list(cur.fetchone()[0] or [])
    merged = sorted(set(current) | set(incoming_skills))
    if merged == sorted(current):
        return  # no new skills -> no-op
    added = sorted(set(merged) - set(current))
    cur.execute("UPDATE candidates SET skills=%s, updated_at=now() WHERE candidate_id=%s",
                (merged, candidate_id))
    _log_change(cur, candidate_id, "skills", current, merged, source,
                source_row_id, batch, "skills_union_add",
                note="added: " + ", ".join(added))
    report.fields_changed += 1

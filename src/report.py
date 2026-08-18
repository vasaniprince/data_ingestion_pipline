"""Run-report accumulator. Matches the Appendix A.4 shape, including the four
counters the brief calls out as the tells for whether the hard parts work:
entities_unchanged_noop, nulls_ignored, out_of_order_records, merges_declined.
"""
from collections import Counter
from datetime import datetime


class RunReport:
    def __init__(self, batch: str):
        self.batch = batch
        self.started_at = datetime.utcnow()
        self.rows_in = 0
        self.rejection_reasons = Counter()
        self.entities_created = 0
        self.entities_updated = 0
        self.entities_unchanged_noop = 0
        self.merges = []            # {candidate_id, rule, rows:[...]}
        self.merges_declined = []   # {rows:[...], reason, detail}
        self.fields_changed = 0
        self.conflicts_resolved = []
        self.out_of_order_records = 0
        self.nulls_ignored = 0
        # enrichment block (filled in Part 4)
        self.enrichment = {
            "budget_calls": 0, "calls_made": 0, "calls_served_from_cache": 0,
            "negative_cache_hits": 0, "spend_inr": 0.0, "saved_inr": 0.0,
            "contactable_before": 0, "contactable_after": 0,
        }

    # -- helpers ------------------------------------------------------------
    def reject(self, reason: str):
        self.rejection_reasons[reason] += 1

    def add_merge(self, candidate_id, rule, rows):
        self.merges.append({"candidate_id": candidate_id, "rule": rule, "rows": rows})

    def add_decline(self, rows, reason, detail):
        self.merges_declined.append({"rows": rows, "reason": reason, "detail": detail})

    def add_conflict(self, field, winner, loser, rule):
        self.conflicts_resolved.append(
            {"field": field, "winner": winner, "loser": loser, "rule": rule})

    @property
    def rows_rejected(self):
        return sum(self.rejection_reasons.values())

    def to_dict(self):
        finished = datetime.utcnow()
        return {
            "batch": self.batch,
            "started_at": self.started_at.isoformat() + "Z",
            "duration_ms": int((finished - self.started_at).total_seconds() * 1000),
            "rows_in": self.rows_in,
            "rows_rejected": self.rows_rejected,
            "rejection_reasons": dict(self.rejection_reasons),
            "entities_created": self.entities_created,
            "entities_updated": self.entities_updated,
            "entities_unchanged_noop": self.entities_unchanged_noop,
            "merges": self.merges,
            "merges_declined": self.merges_declined,
            "fields_changed": self.fields_changed,
            "conflicts_resolved": self.conflicts_resolved,
            "out_of_order_records": self.out_of_order_records,
            "nulls_ignored": self.nulls_ignored,
            "enrichment": self.enrichment,
        }

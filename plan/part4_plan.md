# Part 4 Plan — Enrichment on a budget

## Context

Parts 1-3 built a trustworthy candidate table: one row per person, created once,
updated forever, with provenance. Part 4 adds the one thing that table still
can't do — get in touch with someone. Many candidates have no email or phone
at all; `enrichment_api.json` sells contact lookups at ₹0.09/call with only
**15 calls for the entire assignment** (a global, cumulative budget — not
per-run, not per-batch). The brief is explicit about what's graded:

- **Selection must be justified**, not "the first 15" — use real signal about
  who's worth the spend.
- **A second full pipeline run must cost ₹0.00** — proven by printed spend on
  run 1 vs run 2, not asserted in prose.
- **`not_found` is still a billed call** — cache it too, with a justified TTL.
- **Enrichment is just another source** — its fields merge through the exact
  same Part-3 precedence engine (`merge.apply_scalar`), no special-casing.
- **Report**: calls made, calls saved by cache, ₹ spent, ₹ saved, contactable
  count before vs after.

## What the data actually looks like (verified against the live DB + mock file)

After a full batch_01+batch_02 ingest: **21 candidates**, **6 `contactable=false`**:

- **Tanvi Shah, Gaurav Malhotra, Nikhil Reddy, Rohan Deshpande** — each has a
  `linkedin_handle` identity (`tanvi-shah-ai`, `gaurav-malhotra-aws`,
  `nikhil-reddy-java`, `rohan-deshpande-de`). All 4 keys exist in
  `enrichment_api.json`: 3 `found`, `nikhil-reddy-java` is `not_found`.
- **Siddharth Rao, Divya Menon** — github-only (`github_login`), **no**
  `linkedin_handle` and (by definition of `contactable=false`) no email
  identity either. `enrichment_api.json`'s endpoint is documented as keyed by
  "normalised LinkedIn handle or email" — a github login is not a valid key
  type. Their github logins (`siddrao`, `divyamenon`) happen to be exactly two
  of the mock file's decoy keys, which return `not_found` — a trap for anyone
  who guesses a lookup key from a name/login instead of using a real
  identifier the candidate actually presented. **These two have no legitimate
  key and must never be called.**

So the real eligible pool this run is 4, well under budget — but the selection
and ranking logic must be general (it's graded on the rule, not the count).

## Design decision: decouple "billed" from "merged"

A naive `WHERE contactable=false` selection conflates "never billed" with
"billed but the merge didn't finish" (e.g. a crash between billing and
writing the identity). Fix: two independent, idempotent passes.

- **Billing** is gated on "does `enrichment_cache` already have a row for
  this key" — never re-billed once a row exists, full stop.
- **Merging** is gated on "is there a `found` row with `merged_at IS NULL`" —
  retried every run until it succeeds, regardless of which run billed it.

This needs one small schema addition (idempotent, added to the existing
`migrations/001_init.sql`, no new migration file):

```sql
ALTER TABLE enrichment_cache ADD COLUMN IF NOT EXISTS merged_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_enrichment_cache_unmerged
    ON enrichment_cache (status) WHERE merged_at IS NULL;
```

## New module: `src/enrich.py` (CLI: `python -m src.enrich`)

Reuses existing primitives — no changes needed to `identity.py`/`merge.py`:
`mapping._email_identifier`/`_phone_identifier`, `identity._upsert_identity`,
`identity.finalize_canonical_identities`/`finalize_metrics`,
`merge.apply_scalar`, `report.RunReport` (instantiated as `RunReport("enrichment")`
— its `enrichment` sub-dict already matches the required report shape).

```python
def _load_mock(path=None) -> dict[str, dict]:
    """Load ENRICHMENT_FILE, drop _meta, return cache_key -> response record."""

def _content_hash(payload: dict) -> str:
    """Same sha256(sorted json) algorithm as ingest.py, duplicated locally
    rather than imported (enrich.py shouldn't depend on ingest's private API)."""

def _insert_or_get_enrichment_raw(cur, cache_key, response, fetched_at) -> int:
    """INSERT INTO raw_source_rows (batch='enrichment', source='enrichment',
    source_row_id=NULL, ...) ON CONFLICT (content_hash) DO NOTHING RETURNING id;
    if None (catch-up re-run hit the same content hash), SELECT id FROM
    raw_source_rows WHERE content_hash=%s instead. Always returns a usable
    source_row_pk, even when nothing new was inserted."""

def _skipped_no_key(cur) -> list[str]:
    """candidate_ids with contactable=false and no linkedin_handle identity."""

def _billing_pool(cur) -> list[dict]:
    """candidate_id, cache_key (=linkedin_handle match_key), completeness_score
    for contactable=false candidates whose key has NO row in enrichment_cache
    yet. ORDER BY completeness_score DESC, candidate_id ASC -- this ranking IS
    the selection-justification: a richer, more-observed existing profile is
    more confidently a real, employable candidate worth the spend, vs a thin
    record that's more likely noise."""

def _already_cached_eligible_keys(cur) -> list[dict]:
    """Same shape as _billing_pool but WITHOUT the not-yet-cached filter --
    used only to compute calls_served_from_cache / negative_cache_hits for
    the report (this is what makes run 2's reporting correct: every eligible
    key already has a cache row, so this list *is* the cache-hit list)."""

def _bill(cur, cache_key, candidate_id, mock) -> dict | None:
    """If cache_key not in mock: no-op, return None (nothing to bill for a
    key the mock provider doesn't define). Else INSERT one enrichment_cache
    row (status, response jsonb, candidate_id, cost_inr=config.ENRICHMENT_COST_INR,
    fetched_at=now(), ttl_expires_at=far-future if found / now()+
    ENRICHMENT_NEGATIVE_TTL_DAYS if not_found, merged_at=NULL). Return the row."""

def _merge_found_row(cur, cache_key, candidate_id, response, fetched_at, report):
    """source_row_pk = _insert_or_get_enrichment_raw(...). For work_email/
    personal_email/mobile (whichever non-null): build via mapping._email_identifier
    / _phone_identifier using conf = response.get('confidence') or
    config.SOURCE_CONFIDENCE['enrichment']; identity._upsert_identity(cur,
    candidate_id, ident, 'enrichment', fetched_at). For current_company/
    current_title (if non-null): merge.apply_scalar(cur, candidate_id, field,
    value, 'enrichment', source_row_pk, fetched_at, conf, 'enrichment', report).
    Then UPDATE enrichment_cache SET merged_at=now() WHERE cache_key=%s."""

def run_enrichment(conn) -> dict:
    """
    1. report = RunReport('enrichment'); contactable_before = count(contactable).
    2. skipped_no_key = _skipped_no_key(cur).
    3. pool = _billing_pool(cur); remaining = ENRICHMENT_BUDGET_CALLS -
       (SELECT count(*) FROM enrichment_cache); selected = pool[:remaining],
       skipped_budget = pool[remaining:].
    4. For each selected: row = _bill(cur, key, cid, mock); if row: calls_made+=1,
       spend_inr += row['cost_inr'].
    5. Catch-up merge pass (runs every invocation, unconditionally):
       SELECT cache_key, candidate_id, response, fetched_at FROM enrichment_cache
       WHERE status='found' AND merged_at IS NULL AND candidate_id IS NOT NULL;
       call _merge_found_row for each; collect touched candidate_ids.
    6. Cache-hit accounting for the report: for each row in
       _already_cached_eligible_keys(cur) MINUS pool (i.e. keys that were
       already cached before this run's billing step) -> calls_served_from_cache+=1,
       saved_inr += ENRICHMENT_COST_INR; if that row's status was not_found ->
       negative_cache_hits += 1.
    7. identity.finalize_canonical_identities(cur, touched) +
       identity.finalize_metrics(cur, touched).
    8. contactable_after = count(contactable). Populate report.enrichment dict.
       Write out/run_report_enrichment.json, INSERT INTO run_reports
       (batch='enrichment'), commit. Return payload.
    """

def main(argv) -> int:
    """Mirrors ingest.py's main(): connect, ensure_schema, run_enrichment,
    print json, close."""
```

## DECISIONS.md — new §8 "Enrichment selection & budget"

Document: eligibility requires a *real, previously-observed* linkedin_handle
or email identity (never a guessed/constructed key from name or github
login — cite the `siddrao`/`divyamenon` decoy trap as the reason); ranking by
`completeness_score DESC` as the "worth spending on" justification; budget is
cumulative across the whole `enrichment_cache` table (matches "15 calls for
the entire assignment"); billing vs merging are decoupled for crash-safety;
cross-reference the existing §6 TTL rationale (30 days, already written).

## Tests — `tests/test_enrichment.py` (new)

- `test_no_key_candidates_never_billed`: after batch_01+batch_02 +
  `run_enrichment`, Siddharth/Divya have zero rows in `enrichment_cache` and
  appear in the report's `skipped_no_key`.
- `test_second_run_costs_zero`: run enrichment twice; assert run 2's
  `calls_made == 0` and `spend_inr == 0.0`, while `calls_served_from_cache`
  reflects the previously-billed keys.
- `test_found_result_merges_through_precedence`: Tanvi/Gaurav/Rohan gain an
  `email`/`phone` identity with `source` containing `'enrichment'`; their
  `contactable` flips to `true`; `completeness_score` increases.
- `test_not_found_is_cached_with_ttl`: `nikhil-reddy-java` has exactly one
  `enrichment_cache` row, `status='not_found'`, `ttl_expires_at` ~30 days out;
  Nikhil stays `contactable=false`.
- `test_budget_respected`: monkeypatch `config.ENRICHMENT_BUDGET_CALLS` to 2
  and confirm only 2 candidates get billed, the rest land in `skipped_budget`.

## Files

- NEW `src/enrich.py`
- EDIT `migrations/001_init.sql` (+`merged_at` column, +partial index)
- NEW `tests/test_enrichment.py`
- EDIT `DECISIONS.md` (+§8)
- NEW `plan/part4_plan.md` (copy of this plan, per established convention)

## Verification

1. Fresh truncate + `python -m src.ingest batch_01` + `batch_02` (unaffected
   by this part — confirm counters unchanged from Part 3).
2. `python -m src.enrich` → expect `calls_made=3` (tanvi/gaurav/rohan found +
   nikhil not_found = 4 billed calls, so actually `calls_made=4`),
   `calls_served_from_cache=0`, `negative_cache_hits=1`, `spend_inr=0.36`,
   `saved_inr=0.0`, `contactable_before=15`, `contactable_after=18`.
3. Run `python -m src.enrich` again → `calls_made=0`, `spend_inr=0.0`,
   `calls_served_from_cache=4`, `negative_cache_hits=1`,
   `contactable_before==contactable_after==18`.
4. SQL spot-check: `enrichment_cache` has exactly 4 rows after run 1, still 4
   after run 2; Siddharth/Divya absent from it entirely; Tanvi/Gaurav/Rohan's
   `field_provenance` shows a `source='enrichment'` row wherever the API
   changed `current_title`/`current_company`.
5. `pytest tests/` — full suite (existing 7 + new enrichment tests) green.
# DECISIONS.md — normalization, identity resolution, merge precedence

This is the written policy the code in `src/` obeys. Where the brief left
something ambiguous, the decision and its reasoning are recorded here rather
than only living in a docstring, per "make a decision, write it down, keep
going."

## 1. Normalization rules (`src/normalize.py`)

- **Empty-ish values.** `null`, `""`, `"-"`, `"N/A"`, `"na"`, `"none"`,
  `"not disclosed"`, `"unknown"`, and the LinkedIn placeholder
  `"LinkedIn Member"` all collapse to `None`. None of them are treated as "the
  field became empty" — see §4.
- **LinkedIn URL → handle.** Strip protocol, `www.`/country subdomains (e.g.
  `in.`), query string, and trailing slash; lowercase. `/in/<handle>` is the
  handle. The old `/pub/<name>/<id1>/<id2>/<id3>` form has no real handle, so
  it becomes a stable synthetic key `pub-<name>-<id1>-<id2>-<id3>`. A bare
  `/in/` (no handle at all) normalizes to `None` — this is one of the two
  "pure garbage" rows in batch 1 (row 20).
- **Email → (display, match_key).** `display` is lowercased, `+tag`-stripped,
  domain-normalized, **dots kept**. `match_key` is `display` with dots
  additionally stripped, but **only for `gmail.com`/`googlemail.com`**. The
  brief calls this out explicitly ("those rules are Gmail specific, applying
  them to every domain is a bug") — the dataset tests it directly: Sneha's two
  Deloitte addresses (`sneha.kulkarni@deloitte.com` vs
  `snehakulkarni@deloitte.com`) are genuinely different match keys and stay as
  two distinct email identities on her one candidate; only the gmail variants
  (`Aarav.Mehta@GMAIL.com`, `ishita.rao+jobs@gmail.com`) collapse. `value` (the
  display form) is what a human sees; `match_key` is what identity resolution
  keys on — kept as two separate columns so display and dedupe can never
  silently disagree. Emails on junk domains (`test.com`, `example.*`) are
  treated as no email at all.
- **Phone → E.164, India default.** Strip everything but digits, absorb a
  10-digit number as `+91<digits>`, an 11-digit leading-0 number as
  `+91<remainder>`, otherwise trust an explicit country code. Junk filter: all
  digits identical (e.g. `0000000000`) is not a phone number, even though it's
  syntactically valid E.164 shape — this is the other half of batch 1's
  garbage row (row 21).
- **Name.** Collapse whitespace; flip an obvious `"Last, First"` shape (e.g.
  naukri's `"Kulkarni, Sneha"`) to `"First Last"`. `normalized_name` is the
  lowercased form — used for search and as one leg of the combination-merge
  rule (§2), **never** as a merge key on its own.
- **Experience → months.** `"6+"` floors to 72 (the `+` means "at least 6",
  and flooring is the conservative reading given we can't know the true upper
  bound). `"6 Years 4 Months"` → 76. `"4.5"` → 54 (`4.5 * 12`, rounded).
  `{"years":6,"months":2}` → 74. A bare number is treated as years.
- **Salary → annual INR.** `"38 LPA"` → `num * 100,000`. `"120000 per month"`
  → `num * 12`. A bare number is assumed already-annual. `"Not Disclosed"` →
  `None`.
- **Location.** Parsed into `{raw, city, state, country}` from a small,
  dataset-scoped gazetteer (`CITY_GAZETTEER`) that collapses aliases to one
  canonical city — `"Bangalore"`, `"Bengaluru"`, `"BLR"` all → `city=bengaluru`.
  A real system would call a geocoding service instead (see `INFRA.md`); this
  gazetteer only covers the cities that actually appear in the data. Multi-city
  strings (`"Hyderabad / Bangalore"`) take the first recognized token as the
  primary city; the full string is kept verbatim in `raw`.
- **Skills.** Accepts a list or a comma-separated string (both shapes appear
  within the same source); lowercased, trimmed, deduped, order-preserving on
  first sight. Known near-duplicate spellings (`"power bi"`/`"powerbi"`,
  `"excel"`/`"advanced excel"`, etc.) are collapsed via a curated
  `SKILL_ALIASES` map before dedupe — see §7 for the scope limit of that
  approach.

## 2. Identity resolution (`src/identity.py`), ranked by strength

1. **STRONG, conclusive alone** (DB-enforced via
   `UNIQUE(identity_type, match_key)` in `identities`): `linkedin_handle`,
   `email`, `phone`, `github_login`. Any one of these matching an existing
   identity is sufficient to attach a new observation to that candidate.
2. **COMBINATION, only consulted when zero strong identifiers matched
   anything:** `normalized_name` **AND** a normalized `current_company` key
   **AND** `location_city` **AND** `current_title` (case-insensitive) all
   agreeing with exactly one existing candidate — **three independent
   corroborating signals, not two.** This is the "some identifiers are only
   evidence in combination" case the brief asks for, and its own decline
   example ("different linkedin_handle, company **and city**") implies the
   converse: matching company *and* city *is* meant to count; title is added
   on top as a further guard (see the tradeoff note below). It fires exactly
   once in this dataset — batch 2's naukri row for Vikram Singh (`_row 116`)
   carries no LinkedIn handle and a brand-new email/phone, so it shares no
   strong identifier with the existing Vikram, but its name, company
   ("PhonePe"), city ("Bangalore" → `bengaluru`), and title ("Staff Engineer")
   all match — so it attaches rather than creating a phantom second Vikram.
   The rule requires the incoming record to have company, city, **and**
   title all present (a name-only row never reaches it) and requires the
   match to be **unique** among same-name candidates — an ambiguous match is
   refused rather than guessed at. Any candidate this rule attaches to is
   flagged `combination_match_applied` in `data_quality_flags`, so the
   decision is visible on the record itself, not only in this document.
3. **WEAK, never merges alone:** `normalized_name` by itself. A same-name row
   that fails both (1) and (2) is **declined**: it becomes its own candidate,
   and both the new and the existing same-name candidate are flagged
   `name_collision_reviewed` in `data_quality_flags`. This is why the two Aarav
   Mehtas (Razorpay/Bengaluru vs Maersk/Mumbai — different company **and**
   city **and** title) never merge, while Vikram Singh does: the combination
   rule requires agreement on *three* independent weak signals, and the
   Aaravs disagree on all three. A false merge is unrecoverable and graded as
   the worse error, so name similarity alone is treated as review evidence,
   never as an automatic merge trigger.
4. **Entity merge.** If a single incoming row's identifiers point at *two*
   different existing candidates, they are the same person: repoint every
   child row (identities, raw rows, provenance, change log, enrichment cache)
   from the loser to the survivor, then delete the loser, all in one
   transaction (repoint before delete — never rely on `ON DELETE CASCADE` for
   this, or the loser's history is destroyed instead of preserved). Survivor
   = earliest `first_seen_at`, tie-broken by the lexicographically smaller
   ULID, so the choice is deterministic across re-runs. Not triggered by this
   dataset; implemented because it's a real possibility at 1M-rows/day scale.

**Known tradeoff of rule 2:** two genuinely different people who happen to
share a full name, current employer, *and* city would incorrectly merge. This
dataset doesn't contain that case; if it mattered more, the next signal to add
would be title or a fourth corroborating field before merging on combination
evidence alone.

## 3. Merge precedence (`src/merge.py`) — applied to every field on every re-observation

For each incoming field value against the candidate's current value, in order:

1. **Non-null beats null, always.** An incoming empty value never overwrites
   a populated field. If the field is one we consider "load-bearing"
   (`NULL_PROTECTED_FIELDS` — name, title, company, location, experience,
   salary, notice period) and it currently holds a value, the null is logged
   as a **rejected** change (`REJECTED:null_from_source_is_not_deletion`,
   `applied_at = NULL`) so the decision is auditable, not silent. `open_to_work`
   is deliberately excluded from null-protection: a source simply not
   reporting it isn't a meaningful "we tried and got nothing" signal worth
   logging.
2. **Source trust**, per field (`FIELD_SOURCE_TRUST`):
   - `full_name`: `linkedin_scraper` beats `naukri_export`/`enrichment`
     (tied) beats `github_crawl`. Same reasoning as title/company below —
     LinkedIn is the person's own self-entered profile name; naukri and a
     verified-enrichment record are both reasonable but secondary; a github
     login/display name is the least likely to be a person's real full name.
   - `current_title` / `current_company`: `linkedin_scraper` beats
     `naukri_export` beats `github_crawl`. LinkedIn is the person's own
     current self-description; naukri's designation/employer strings are
     often the more corporate-formatted variant (e.g. "Razorpay Software Pvt
     Ltd" vs "Razorpay"); github rarely carries a real title at all.
   - `experience_months` / `annual_salary_inr` / `notice_period_days`: naukri
     beats linkedin beats github. Naukri's `total_exp`/`annual_salary` fields
     are structured HR data (`"6 Years 4 Months"`), more precise than
     linkedin's vaguer `"6+"`.
   - `location`: linkedin beats github beats naukri. Naukri's field is
     `pref_location` — a *preference*, not necessarily where the person
     currently is — so it's the least trusted source for **current**
     location, even though it's the most trusted source for salary.
   - `email` / `phone`: enrichment beats naukri beats linkedin beats github
     (enrichment is a paid, purportedly-verified provider).
   - `open_to_work`: all sources tied — this is a volatile, fast-changing
     signal, so the tie-break (recency, next) is the only rule that makes
     sense for it.
3. **Recency**, on `_crawled_at` — compared against the *stored* observation
   timestamp for that field, never against file/arrival order. This is what
   makes out-of-order arrival safe: a batch-2 row with an earlier
   `_crawled_at` than what's already recorded cannot revert a same-or-higher
   trust field, because the comparison is time-based, not "whichever batch
   ran last." `out_of_order_records` counts any attach where the incoming
   `_crawled_at` is older than the candidate's current `last_seen_at`; the
   count is not evidence of a bug — it's the "we saw this and handled it
   correctly" trace the run report needs.
4. **Confidence** — final tiebreaker (per-source defaults: linkedin 0.90,
   naukri 0.80, github 0.70, enrichment uses the API's own value). Only
   reached if trust and recency both tie, which doesn't happen in this
   dataset.
5. Still tied → keep the incumbent. Deterministic and idempotent by
   construction — the same inputs in any order produce the same outcome.

**Identical value → write nothing.** Before any of the above runs, an
incoming value that equals the current value (for `location`, compared on
`city`/`state`/`country` only — a different spelling of the same place is not
a change) is a true no-op: no `field_provenance` row, no `change_log` row.
This is the requirement most naive implementations fail, and it's what makes
`entities_unchanged_noop` and the double-run idempotency test meaningful.

**Skills are a union, not a precedence field.** Each source's skill list is
added to a running, sorted, deduped set that only grows. A source reporting
fewer skills than we already know is not "the person lost a skill" — it's
"this crawler didn't ask about that skill."

## 4. "Null is not deletion"

A source returning `null`/empty for a field we already have a value for is
evidence that *that source didn't observe or doesn't carry that field* — not
evidence the fact changed. It is refused and logged as a REJECTED change
(§3.1), counted in `nulls_ignored`, and the existing value is left untouched.

## 5. Idempotency

Two layers:
- **Optimization:** `raw_source_rows.content_hash` (SHA-256 of the
  canonicalized payload) is `UNIQUE`; re-feeding byte-identical raw JSON is
  skipped before any processing (`ON CONFLICT DO NOTHING`). Cheap, but only
  catches literally-identical re-crawls.
- **Correctness guarantee:** the value-based merge itself (§3) is what
  actually guarantees "re-run changes nothing." Even if a re-crawl differs
  slightly at the byte level (whitespace, key order, a refreshed timestamp)
  but resolves to the same field values, the no-op path (§3, "identical
  value") still writes zero rows. The double-run test in `tests/` exercises
  this guarantee directly, not just the hash shortcut.
- The idempotency assertion compares business columns only — audit-only
  timestamps (`ingested_at`, `recorded_at`, `created_at`, `updated_at`) and
  the append-only `run_reports` table (one legitimate new row per run) are
  intentionally excluded.

## 6. Enrichment cache TTL

Positive (`found`) results are cached with no expiry — a verified email/phone
doesn't go stale within our run horizon. Negative (`not_found`) results are
still a billed call, so they're cached too, with a **30-day** TTL: long enough
that we never re-bill for the same miss within the same work cycle, short
enough that a profile which later adds contact info isn't permanently written
off.

## 7. Known tradeoffs / open gaps

- **The combination merge rule (§2.2) can still, in principle, false-merge two
  different people who share name, employer, city, AND title.** Requiring a
  third signal (title, added on top of company+city) makes the coincidence
  meaningfully less likely than the original 2-signal design, and any
  candidate it attaches to is flagged `combination_match_applied` so the
  decision is auditable/reviewable rather than silent — but it does not reach
  zero risk, and a system processing enough volume will eventually hit that
  coincidence. The residual mitigation, not yet built, is a review queue: flag
  instead of auto-attach when the match is combination-only, and let a human
  or a higher-confidence signal (e.g. a later enrichment call) confirm it.
- **Skill-token near-duplicates are now canonicalized** via a small alias map
  (`SKILL_ALIASES` in `src/normalize.py`) — `"powerbi"` → `"power bi"`,
  `"advanced excel"` → `"excel"`, `"postgres"` → `"postgresql"`, etc. This is
  a curated, dataset-informed list, not a general skills taxonomy; skills
  outside it that use different but equivalent phrasing will still sit as
  separate tokens. A production system would back this with a real skills
  ontology (e.g. ESCO or a vendor taxonomy) instead of a hand-maintained map.
- The location gazetteer is scoped to this dataset's cities, not a general
  geocoder.
- See `WRITEUP.md` for the full list of hostile cases caught vs. not handled.

## 8. Enrichment selection & budget (`src/enrich.py`)

The brief sells 15 calls total for the **entire assignment** — a global,
cumulative budget, not per-run or per-batch — and explicitly grades the
selection rule ("not the first 15") and whether a second run costs Rs 0.00.

- **Eligibility requires a real, previously-observed identifier.** The mock
  API is documented as keyed by "normalised LinkedIn handle or email" — never
  a name or a github login. Two candidates in this dataset (Siddharth Rao,
  Divya Menon) are `contactable=false` but have only a `github_login`; the
  mock file even contains decoy entries shaped exactly like their github
  logins (`siddrao`, `divyamenon`) that resolve to `not_found` — a trap for
  code that guesses a key from a name/login instead of using an identifier
  the candidate actually presented. Both are skipped and reported under
  `skipped_no_key`, never billed.
- **Selection rule**, applied to the remaining eligible pool (`contactable=false`
  AND has their own `linkedin_handle`): rank by `completeness_score DESC`,
  candidate_id ASC as a tiebreak. A richer, more-observed existing profile is
  more confidently a real, employable candidate worth Rs 0.09, versus a thin
  record that's more likely noise — this is the "who looks employable, who is
  worth spending on" judgment the brief asks to be written down. Take as many
  off the top as the remaining global budget allows; the rest are reported
  under `skipped_budget`.
- **Billing vs. merging are decoupled.** `enrichment_cache` gates *billing*
  (a key with any row, `found` or `not_found`, is never billed again — this
  alone is what makes a second full run cost Rs 0.00). A separate
  `merged_at` column gates *merging*: a `found` row with `merged_at IS NULL`
  is retried every run until its fields are actually written onto the
  candidate. This means a crash between billing and merging self-heals on
  the next run instead of silently re-billing or silently losing the result.
- **Enrichment is just another source.** Found `current_title`/`current_company`
  go through the exact same `merge.apply_scalar` precedence engine as
  linkedin/naukri/github (§3) — no bypass. In this dataset those values match
  what's already on file, so the no-op path correctly writes nothing (proof
  the engine isn't blindly overwriting). Found email/phone go through the
  same `identity._upsert_identity` every other source uses, tagged
  `source="enrichment"`.
- **Negative TTL** is the same 30 days already justified in §6.

## 9. CSV export column derivations (`src/export.py`)

`out/candidates_enriched.csv` reads precomputed state only — it never
recomputes `completeness_score`, `contactable`, `source_count`,
`identity_count`, or canonical identities; those are maintained by
`identity.finalize_metrics`/`finalize_canonical_identities` at ingest/
enrichment time (§2-§3). A few columns need an explicit rule at export time:

- **`primary_email` / `primary_phone`** = the `identities.value` row with
  `is_canonical=TRUE` for that type (at most one per type, guaranteed by
  `finalize_canonical_identities`); empty string if the candidate has none.
- **`enriched`** = the candidate has an `enrichment_cache` row with
  `status='found'` that has actually been merged (`merged_at IS NOT NULL`) —
  a billed-but-not-yet-merged row (shouldn't happen post-`finalize`, but see
  Part 4's crash-safety design) does not count as enriched.
- **`enrichment_cost_inr`** = the **actual sum** of `cost_inr` across *all*
  of that candidate's `enrichment_cache` rows, found or not_found. A paid
  miss still cost money and the CSV says so: Nikhil Reddy (not_found) shows
  `enriched=false, enrichment_cost_inr=0.09`, not `0.00`. This is a
  deliberate accountability choice — the brief's own two-row example only
  illustrates the found/never-called cases, but "is the enrichment spend
  defensible" (the brief's judging language) argues for the honest number,
  not one that hides a paid miss behind a boolean.
- **`skills`** = the stored array, semicolon-joined, in existing (first-seen)
  order — no re-sorting at export time.
- **`open_to_work`** (and any other nullable boolean) renders `false` when
  NULL. An unobserved signal isn't evidence the person is closed to offers,
  but the brief's CSV example never shows a blank boolean cell, and a CSV
  consumer (a recruiter's spreadsheet) is better served by a concrete value
  than an ambiguous empty cell.
- **`city`/`country`** flatten the nested `location` (kept nested in the API
  response, per Appendix A.2) to just those two fields, per Appendix A.3.
- Row order is `ORDER BY candidate_id` (ULIDs sort chronologically by
  creation), making the file byte-identical across re-runs against an
  unchanged table.

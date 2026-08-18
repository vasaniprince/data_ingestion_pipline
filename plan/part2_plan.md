# Part 2 Plan — Ingest batch 1 (create) + the shared ingest engine

## Context

Part 1 shipped the schema. Part 2 builds the ingest layer that turns messy raw
rows into trustworthy canonical people: **normalise → resolve identity →
upsert (merge) → report**. The deliverable is a working ingest of
`batch_01_raw.jsonl` that creates one canonical row per real person, with
provenance and a change log, plus `out/run_report_batch01.json`.

Batch 1 is "create", but it is **not** pure inserts: three rows merge into a
candidate created earlier in the same batch (Aarav, Sneha, Arjun), which
exercises the field-merge + change-log path. So Part 2 builds the **complete
ingest engine** once; Part 3 (batch 2) simply re-runs it and adds the
idempotency test + `DECISIONS.md`. This avoids rework and is the only way batch 1
comes out correct.

> First execution step: also copy this plan to `plan/part2_plan.md` in the repo
> (as with Part 1). The Part 1 plan is already saved at `plan/part1_plan.md`.

---

## What batch_01 must produce (independently verified trace)

These are the graded numbers; the run report and a test will assert them.

- **rows_in: 21**
- **rows_rejected: 2** — row 20 (`/in/` → empty handle, placeholder name "LinkedIn Member", no id) and row 21 (name "-", `test@test.com` junk email, `0000000000` junk phone → no usable id). Reasons distinct: `no_usable_identifier` (20) and `junk_only_row` (21).
- **entities_created: 16**
- **merges: 3**, all rule `linkedin_handle_exact`: rows `[1,9]` (aarav-mehta-b12a4), `[4,14]` (sneha-kulkarni-analyst), `[8,15]` (arjun-pillai-recsys — handle extracted from github `blog` on row 15).
- **merges_declined: 1** — candidate{1,9} vs candidate{19}: same `normalized_name` "aarav mehta", different handle/email/company/city → `name_match_only_insufficient`. Both flagged `name_collision_reviewed`.
- **Aarav field conflicts** (rows 1 vs 9): title → "Senior Software Engineer" (linkedin trust), company → "Razorpay" (linkedin trust), experience → **76 months** (naukri trust, beats linkedin "6+"→72), location → bengaluru (both agree), salary 3,800,000 + notice 60 (naukri only). skills = 7-way union.
- **Sneha keeps TWO deloitte emails** (`sneha.kulkarni@` + `snehakulkarni@`) — dots are gmail-only, so they stay distinct identities; the merge rides the shared linkedin_handle. This is the "gmail rules are gmail-specific" test.
- **Contactless-but-valid survive:** Siddharth (row 16, github_login only), Rohan/Vikram/Tanvi (handle only, no email/phone). A strong id ≠ contactable; enrichment fills contact later.

---

## Architecture / modules (all under `src/`)
- `normalize.py` *(exists — extend, see below)*: pure field normalisers.
- `ids.py` *(new)*: minimal ULID (`cand_`/`chg_`/`run_` prefixes). Sortable, matches Appendix `cand_01J9X…`. No external dep.
- `db.py` *(new)*: psycopg connection + a `run_migration()` that applies `migrations/001_init.sql`. Raw SQL throughout (reviewable; no ORM).
- `mapping.py` *(new)*: per-source adapters (`linkedin_scraper`, `naukri_export`, `github_crawl`) → one `Record` dataclass: `{identifiers: [(type, display, match_key, raw_form, confidence)], fields: {canonical_field: (value, confidence)}, crawled_at, source, batch, source_row_pk}`. Isolates each source's weird field names in one place.
- `identity.py` *(new)*: resolve a `Record`'s identifiers against `identities`; return matched candidate_id(s); create/attach/entity-merge; decline name-only.
- `merge.py` *(new)*: per-field precedence resolution + change detection (writes `field_provenance` + `change_log` only when the value actually changes).
- `ingest.py` *(new)*: orchestrator + CLI. `python -m src.ingest batch_01` (path from `data/`). Reads jsonl → sorts by `(crawled_at, source_row_id)` → per row: insert raw (content-hash dedupe), map, normalise, reject-check, resolve, create/update, provenance, change_log → accumulates the run report → writes `out/run_report_batchNN.json` + a `run_reports` row.
- `report.py` *(new)*: the run-report accumulator (all Appendix A.4 counters).

## Source → canonical field mapping
| canonical | linkedin_scraper | naukri_export | github_crawl |
|---|---|---|---|
| identifiers | `profile_url`→linkedin_handle; `email`; `phone` | `linkedin`→handle; `email_id`; `mobile` | `login`→github_login; `blog`→linkedin_handle (if LI URL); `email` |
| full_name | `full_name` | `candidate_name` | `name` |
| current_title | `current_title` | `curr_designation` | — |
| current_company | `current_company` | `curr_employer` | `company` (strip leading `@`) |
| location | `location` | `pref_location` *(preferred, see note)* | `location` |
| skills | `skills` (list) | `key_skills` (list OR csv string) | `top_languages` (list) |
| experience_months | `experience_years` | `total_exp` | — |
| annual_salary_inr | — | `annual_salary` | — |
| notice_period_days | — | `notice_period` | — |
| open_to_work | `open_to_work` | — | `hireable` |
| crawled_at | `_crawled_at` | `_crawled_at` | `_crawled_at` |

## Normalization additions to `src/normalize.py`
- **`normalize_email` → return `(display, match_key)`**: display = lowercased, plus-tag stripped, domain normalised, **dots kept**; match_key = display but **dots stripped for gmail/googlemail only**. Add junk filter: reject `test.com`/`example.*` domains → `(None, None)`. (Feeds `identities.value` vs `match_key`.)
- **`normalize_phone`**: add junk filter — all-zeros / single-repeated-digit → None. (Kills row 21's `0000000000`.)
- Keep existing URL/name/location/skills/experience/salary/notice logic; verify `"6+"`→72 (floor, `+` = "at least").

## Identity resolution — ranked rules (`identity.py`)
**STRONG (conclusive alone; DB-enforced via `UNIQUE(identity_type, match_key)`):**
1. `linkedin_handle_exact`
2. `email_exact` (on match_key)
3. `phone_exact` (E.164)
4. `github_login_exact`

Algorithm per record: look up each strong identifier → set of existing candidate_ids.
- **0 matches →** create candidate (new ULID), insert identities, write field_provenance for every present field (creation writes **no** change_log rows).
- **1 match →** attach any new identifiers to that candidate, then run the merge engine on its fields (this is the intra-batch merge path).
- **≥2 matches →** the record bridges two candidates who are one person → **entity merge**: survivor = `MIN(first_seen_at)` then `MIN(candidate_id)`; repoint identities/raw/provenance/change_log/enrichment from loser→survivor in one txn, then delete loser; re-resolve fields. *(Not triggered by this dataset — implemented for robustness/1M-scale; logged if it fires.)*

**WEAK (never merges): `normalized_name`.** If a record shares `normalized_name` with an existing candidate but no strong id matches → **decline**: create a separate candidate, add `name_collision_reviewed` to both `data_quality_flags`, append to `merges_declined`. A false merge is unrecoverable and graded as the worse error, so name is treated as review-evidence only, never an auto-merge.

**REJECT:** no usable strong identifier after junk-strip → mark `raw_source_rows.rejected=true` + reason, never create a candidate. (Name-only rows are un-dedupable and un-contactable → rejected.)

## Merge / upsert engine — field precedence (`merge.py`)
For each incoming field value vs the candidate's current value:
1. **Non-null beats null** — an incoming null/empty **never** overwrites a populated field. If incoming is explicitly null for a currently-populated tracked field → write a REJECTED change_log row (`rule=REJECTED:null_from_source_is_not_deletion`, `applied_at=NULL`) and `nulls_ignored++`. *(Fires in Part 3, e.g. Rohan row 103; ~0 in batch 1.)*
2. **Source trust** for that field (see table below) — higher trust wins.
3. **Recency** — tie on trust → newer `crawled_at` wins. (Compared on `crawled_at`, not arrival order → out-of-order rows can't revert newer same-trust values.)
4. **Confidence** — final tiebreaker.
5. Still tied → keep incumbent (stable/deterministic).
- **skills** = sorted, deduped **union** (never shrinks).
- On a win: supersede the current `field_provenance` row (set `is_current=false`, `reason`), insert the new current row, write an **applied** change_log row. On identical value: **write nothing** (the no-op requirement). Losers are not stored as provenance rows but are counted in `conflicts_resolved` and remain in `raw_source_rows`.

## The three gap-decisions (→ DECISIONS.md)
- **Per-source base confidence:** linkedin 0.90, naukri 0.80, github 0.70, enrichment = the API's own `confidence`. (Roughly matches Appendix; values are illustrative per the brief.)
- **Field source-trust ranks** (lower = wins): title/company `linkedin<naukri<github`; experience/salary/notice `naukri<linkedin<github`; **location `linkedin<github<naukri`** (naukri is `pref_location`, aspirational, so least trusted for current location); email/phone `enrichment<naukri<linkedin<github`; open_to_work all-equal → recency wins (most recent signal for a volatile field). *(Config already stubbed in `src/config.py`; reconcile to these.)*
- **`"6+"`** → floor to 72 months.

## Run report (batch_01) — Appendix A.4 shape
All counters emitted, including the four hard ones: `entities_unchanged_noop`, `nulls_ignored`, `out_of_order_records`, `merges_declined`. Enrichment block present but zero (Part 4). Written to `out/run_report_batch01.json` and a `run_reports` row.

## CLI + requirements
- `requirements.txt`: `psycopg[binary]` (v3), `python-dotenv` (optional). FastAPI etc. added in Part 5.
- Entrypoint: `python -m src.ingest batch_01` — env `DATABASE_URL` (default in `src/config.py`). Auto-runs the migration if tables are absent.

## Verification (Part 2 done =)
1. `docker run` throwaway Postgres 16 (as in Part 1), apply migration, `python -m src.ingest batch_01`.
2. Assert from `out/run_report_batch01.json`: `rows_in=21`, `rows_rejected=2`, `entities_created=16`, `len(merges)=3` all `linkedin_handle_exact`, `len(merges_declined)=1`.
3. Spot-check **Aarav #1** via SQL: one candidate, identities = {handle aarav-mehta-b12a4, email aaravmehta@gmail.com, phone +919845098450}, `experience_months=76`, `current_title="Senior Software Engineer"`, `current_company="Razorpay"`, skills has all 7. Provenance: `current_title` current row = linkedin; `experience_months` current row = naukri.
4. Spot-check **both Aarav Mehtas exist** as separate candidates, each with `name_collision_reviewed`. **Sneha** has 2 distinct deloitte email identities + 1 handle + 1 phone.
5. Confirm rows 20 & 21 are in `raw_source_rows` with `rejected=true` and **no** candidate.
6. `change_log` has applied rows for the intra-batch fills (Aarav email/phone/salary/notice/skills/experience) and **zero** rows for any field that didn't change.

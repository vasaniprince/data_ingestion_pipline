# Part 3 Plan — Ingest batch 2 (update) + combination merge + idempotency test + DECISIONS.md

## Context

Part 2 built the full ingest engine and it already handles most of batch 2
(attach, field precedence, skills union, null-protection, no-op, change log).
But an independent trace of batch 2 against the engine exposed one real gap that
the assignment is specifically probing:

**Row 116 (Vikram Singh, naukri) does not link to the existing Vikram.** It
carries no LinkedIn handle, and its email/phone are brand-new, so it shares no
*strong* identifier with the batch-1 Vikram (who had only a LinkedIn handle).
The strong-id-only engine therefore creates a duplicate Vikram, and the
assignment's **out-of-order case never triggers** (`out_of_order_records = 0`).
The brief lists that counter as one of the four tells that the hard parts work,
and its decline example cites "different linkedin_handle, company **and city**"
— i.e. matching company+city *is* intended as combination evidence.

**Decision (user deferred to the assignment):** add a conservative
**name + company + city** combination-merge rule. It links row 116 to the real
Vikram (→ exercises out-of-order handling, null-protection, and salary/notice
fills on the real record) while staying safe on the graded false-merge case —
the two Aaravs differ on company AND city, so they still never merge. This is
the intended "evidence in combination" path; name *alone* still never merges.

Part 3 = that one engine change + a small location-equality fix + `DECISIONS.md`
+ the idempotency test, then produce `out/run_report_batch02.json`.

> First execution step: copy this plan to `plan/part3_plan.md` in the repo.

---

## Engine change 1 — combination merge (`src/identity.py`)
Add `_combination_match(cur, rec)`: only consulted when there are **zero** strong
matches. Query candidates by `normalized_name` (indexed); among them, return the
one whose `normalize_company_key(current_company)` **and** `location_city` both
equal the record's — but only if the record itself has a non-null company **and**
city, and exactly one candidate matches (ambiguous → no match). In
`process_record`, insert this between the strong-match branch and create:
- 0 strong matches **and** a combination match → `_attach(..., rule="name_company_location")`.
- 0 strong matches **and** no combination → `_create_candidate` (which still runs
  the same-name check → decline + `name_collision_reviewed` flag when a name
  twin exists, e.g. the two Aaravs).

This is the only place name+company+city can merge; every stronger path already
short-circuits it. Residual risk (two distinct people sharing name+company+city)
is documented in DECISIONS.md as a deliberate tradeoff.

## Engine change 2 — location equality by components (`src/merge.py`)
`apply_scalar` currently compares the whole location dict, so row 103 (Rohan:
"Pune, MH" → "Pune, Maharashtra, India", identical city/state/country) logs a
churn change. Compare location on `(city, state, country)` only; if those are
equal it is a no-op (raw spelling differences don't count as a field change).
Consequence: Rohan's batch-2 re-crawl becomes effectively a no-op (its only real
content is two nulls we correctly refuse) → cleaner counters, less churn.

## What batch_02 must produce (verified trace, WITH the two changes)
- **rows_in 16, rows_rejected 0.**
- **entities_created 5** — Lakshmi (rows 108+115, one candidate via linkedin_handle), Gaurav (109), Nikhil (114), Neha (110), Divya (111).
- **entities_updated ~8** — Aarav#1 (101: title→"Staff Engineer" `newer_observation_same_source`, +kubernetes), Ishita (102: title/company→Kagi Labs, open_to_work→false, +ray), Vikram (104 +gRPC/open_to_work→true; **116 combination-merge** → salary 6,200,000 & notice 90 fill, exp→99 naukri trust, +email/phone identities), Zoya (112 **email bridge** github→linkedin, company→Self-employed, +5 skills), Sneha (106 exp/salary/notice newer, +dbt, +gmail identity), Aarav#2 (113 **email bridge**, exp→110, salary/notice fill), Arjun (107 open_to_work→false).
- **entities_unchanged_noop 2** — Tanvi (105, pure no-op) and Rohan (103, effective no-op after the location fix).
- **out_of_order_records 1** — row 116 (crawled 2026-08-09 < Vikram's last_seen 2026-08-10); its older crawl must NOT revert Vikram's newer linkedin title/company (it doesn't — those stay linkedin by source-trust; salary/notice only *fill* empties).
- **nulls_ignored 2** — row 103 Rohan: `current_title` and `current_company` incoming null vs populated → REJECTED change_log rows.
- **merges** includes Vikram with rule `name_company_location`, rows [5,104,116]; the email-bridge attaches (Zoya, Aarav#2) with rule `email_exact`.
- **merges_declined 0** in batch 2 (the [1,19] Aarav decline already happened in batch 1).

## Idempotency test (`tests/`) — the graded double-run
`tests/test_idempotency.py` (pytest, also runnable via `python -m`):
1. TRUNCATE all tables; run batch_01 then batch_02.
2. Snapshot the **data** tables via a deterministic digest — business columns
   only, excluding volatile audit fields (`ingested_at`, `recorded_at`,
   `created_at`, `updated_at`) and ordering rows stably; record `change_log`
   row count = N.
3. Run batch_01 then batch_02 **again**.
4. Assert: digest identical, `change_log` count still N (zero new rows), and the
   second runs' reports show `entities_created=0`, `fields_changed=0`.

Plus `tests/test_resolution.py` asserting the graded cases: two Aaravs stay
separate (both flagged), Vikram is a single candidate after 116 (rule
name_company_location), Sneha keeps two distinct deloitte emails, rows 20/21
rejected. Add `pytest` to `requirements.txt`.

## DECISIONS.md (new, repo root)
Written policy the code obeys: (1) normalization rules per field incl. gmail
dot/plus is gmail-only, `"6+"`→72, empty-ish set, junk email/phone; (2) identity
resolution ranked — strong ids conclusive, name+company+city combination, name
alone never merges, entity-merge survivor rule; (3) **merge precedence**
non-null>source-trust>recency(crawled_at)>confidence, with the per-field trust
table and why (title/company→linkedin, experience/salary/notice→naukri,
location→linkedin over naukri's pref_location); (4) null-from-source is not a
deletion; (5) out-of-order handled by comparing crawled_at, and why trust-first
still protects the fields that matter; (6) no-op writes nothing; (7) negative
enrichment TTL 30 days; (8) idempotency = content-hash dedupe + value-based
merge; (9) known tradeoffs (combination false-merge risk, skill-token
near-duplicates like "power bi"/"powerbi").

## Files
- `src/identity.py` — add `_combination_match`, wire into `process_record`.
- `src/merge.py` — location equality by (city,state,country).
- `tests/test_idempotency.py`, `tests/test_resolution.py` (new); `tests/__init__.py`.
- `DECISIONS.md` (new); `requirements.txt` (+pytest); `plan/part3_plan.md`.

## Verification
1. Recreate a clean DB (the persistent `saral_pg` is fine — the test TRUNCATEs).
2. `python -m src.ingest batch_01 && python -m src.ingest batch_02`; assert
   batch_02 counters above (esp. `out_of_order_records=1`, `nulls_ignored=2`,
   `entities_unchanged_noop=2`, Vikram merged via `name_company_location`).
3. SQL spot-checks: exactly one Vikram with salary 6,200,000 + email/phone
   identities; Aarav#1 title now "Staff Engineer" with a superseded
   provenance row; Rohan still company "Zeta"/title "Data Engineer II" (nulls
   refused); a change_log REJECTED row for each refused null.
4. Re-verify batch_01 is unchanged by the two engine edits (still 16 created,
   1 decline).
5. `pytest tests/` green — the double-run leaves the table identical and writes
   zero new change rows.

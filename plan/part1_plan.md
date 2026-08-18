# Part 1 Plan — Postgres Schema (`migrations/001_init.sql`)

## Context

SARAL's ingest pipeline currently just *appends*, so one real person lands as
several rows and enrichment spend is wasted re-fetching known people. Part 1 is
the **foundation** the other six parts stand on: a schema where there is one
canonical row per person, the raw rows that produced it are never thrown away,
every field is traceable to a source, and "why does this row say this?" is
answerable in **one query**. Idempotency, merge-precedence, and enrichment
caching (Parts 2–4) all depend on getting this schema right first.

A draft `migrations/001_init.sql` already exists but was written before the
design was fully thought through. Two independent design reviews found it
**diverges from the intended schema** — missing contract columns and a
non-unique invariant index. This plan specifies the corrected target schema and
exactly what to change. **Deliverable of Part 1 = the migration file only.** The
ingest *behaviour* that relies on these columns is Parts 2–3; here we only note
the "schema contracts" the later code must obey.

> First execution step: also save this plan to `plan/part1_plan.md` inside the
> repo (the user asked for it there), then apply the migration changes.

---

## Target tables (7)

### 1. `raw_source_rows` — every raw row, forever (provenance + idempotency anchor)
- `id BIGINT GENERATED ALWAYS AS IDENTITY PK`, `batch`, `source`, `source_row_id INT` (the `_row`), `crawled_at TIMESTAMPTZ` (the `_crawled_at`), `raw_payload JSONB` (untouched original), `candidate_id TEXT` (nullable, set after resolution; NULL for rejected/garbage rows), `rejected BOOL`, `rejection_reason TEXT`, `ingested_at TIMESTAMPTZ DEFAULT now()`.
- **ADD `content_hash TEXT NOT NULL UNIQUE`** = SHA-256 over the canonicalised `raw_payload` (sorted keys). This is the real idempotency anchor and it covers the 1M/day case where `_row` may be absent — the current `WHERE source_row_id IS NOT NULL` partial unique dedupes *nothing* when `_row` is missing.
- Keep `UNIQUE(source, source_row_id) WHERE source_row_id IS NOT NULL` as a natural-key backstop, but **drop `batch` from it** (same logical row re-fed under a different batch label should still dedupe).

### 2. `candidates` — one canonical row per person
- Identity/attrs: `candidate_id TEXT PK` (`'cand_' + ULID`, sortable, matches Appendix `cand_01J9X…`), `full_name`, `normalized_name`, `current_title`, `current_company`, `location_raw/_city/_state/_country`, `experience_months INT`, `annual_salary_inr BIGINT`, `notice_period_days INT`, `skills TEXT[]` (union, **stored sorted+deduped**), `open_to_work BOOL`, `data_quality_flags TEXT[]`.
- **ADD maintained-on-write metrics** (so serve/CSV are O(1), not per-read aggregations — the 1M/day choice): `source_count INT`, `identity_count INT`, `completeness_score NUMERIC(4,3)`, `contactable BOOL`.
- `first_seen_at`, `last_seen_at`, `created_at`, `updated_at`.
- **REMOVE `name_collision_reviewed BOOLEAN`** — Appendix A.3 puts it as a *value inside `data_quality_flags`* (e.g. the supply-chain Aarav row), not a column.
- **Schema contracts for the code:** `last_seen_at = max(crawled_at)` from data (stable across replays, never `now()`); `updated_at` bumped **only** when a field actually changes; `skills` always re-sorted before write — otherwise the row isn't byte-identical on re-run and idempotency breaks.

### 3. `identities` — "these identifiers are the same human"
- `id`, `candidate_id FK`, `identity_type` CHECK in (`linkedin_handle`,`email`,`phone`,`github_login`), `value` (canonical **display** value), **`match_key`** (dedup key), `raw_forms TEXT[]`, `observed_in TEXT[]`, `first_seen_at`.
- **ADD `email_type TEXT` (work/personal, nullable), `confidence NUMERIC(4,3)`, `is_canonical BOOL`** — required by Appendix `emails[].type/confidence`, `phones[].confidence`, `identities[].canonical`.
- **`value` vs `match_key`:** Gmail dot-stripping applies to the *match key only*. Appendix displays `aarav.mehta@gmail.com` (dots kept) while the same mailbox must dedupe against dot/plus variants. So `value` = lowercased, plus-tag stripped, dots **kept**; `match_key` = additionally dots stripped for gmail. `UNIQUE(identity_type, match_key)`.
- **This UNIQUE is the identity-resolution backbone:** one identifier → ≤1 candidate, enforced by the DB. Insert as `ON CONFLICT (identity_type, match_key)`; "existing row points to a *different* candidate_id" is itself the **entity-merge trigger** (see below).

### 4. `field_provenance` — per-field "why", append-only history
- `id`, `candidate_id FK`, `field_name`, `value JSONB`, `source`, `source_row_id BIGINT FK → raw_source_rows`, `observed_at`, `confidence`, `note`, `is_current BOOL`, `recorded_at`.
- **ADD `reason TEXT`** — Appendix `superseded[].reason` (e.g. `newer_observation`) has no home otherwise; `note` is already used for parse notes (`"parsed from '38 LPA'"`).
- **CRITICAL: make the current index `UNIQUE`** → `CREATE UNIQUE INDEX … ON field_provenance (candidate_id, field_name) WHERE is_current`. Enforces exactly-one-current-per-field, makes the one-query provenance read correct, and forces the merge code to re-resolve precedence per field after an entity merge. The draft has it as a plain (non-unique) index — the top defect.
- **Schema contract:** provenance writes are change-gated (never append on a no-op) → `superseded[]` never shows spurious same-value entries.

### 5. `change_log` — every applied/rejected decision (zero rows on true no-op)
- `event_id TEXT PK` (`'chg_' + ULID`), `candidate_id FK`, `field_name`, `old_value JSONB`, `new_value JSONB`, `source`, `source_row_id FK`, `batch`, `rule TEXT`, `note TEXT`, `applied_at TIMESTAMPTZ` (**NULL = rejected**, e.g. `REJECTED:null_from_source_is_not_deletion`).
- **Idempotency guard (critical for the double-run test):** the rejected-null and out-of-order cases (Rohan row 103 null company; Vikram row 116 older crawl) would otherwise re-log on every batch-2 replay. Anchor idempotency on `content_hash` (re-ingest of an identical raw row is skipped entirely, so no re-logging) **and** add `UNIQUE(candidate_id, field_name, source_row_id, rule)` as a belt-and-braces guard so the same decision can't be written twice.
- **A true no-op (incoming value identical to current) writes NOTHING here** — the requirement most implementations fail.

### 6. `enrichment_cache` — pay once per key (Part 4)
- `cache_key TEXT PK` (the linkedin handle or email we called with), `status` (found/not_found), `response JSONB`, `candidate_id FK`, `cost_inr NUMERIC(6,2)`, `fetched_at`, `ttl_expires_at`.
- Negative (`not_found`) results cached too, with TTL (default 30 days — see DECISIONS.md); positive results effectively permanent for our horizon.

### 7. `run_reports` — GET /stats source of truth
- `run_id TEXT PK`, `batch`, `started_at`, `finished_at`, `report JSONB` (the full Appendix A.4 run report).

---

## Idempotency model (schema-level)
- **Anchor:** `content_hash` UNIQUE + `ON CONFLICT DO NOTHING` → identical re-fed row skipped entirely. This is an *optimization*.
- **Real guarantee:** value-based merge — a change is written only when the resolved value differs — so re-processing produces zero changes even if the hash differs (whitespace/key-order re-crawl). State both in WRITEUP; the value-based one is the sophisticated answer.
- **Idempotency assertion scope:** the test compares the *data* tables (`candidates`, `identities`, `field_provenance`, `change_log`). `run_reports` is an append-only audit log (each run legitimately emits one) and volatile audit timestamps (`ingested_at`, `recorded_at`) are excluded — documented in DECISIONS.md. `raw_source_rows` *content* is identical because `content_hash` blocks re-insertion.

## Entity merge (schema readiness)
- When one raw row's identifiers resolve to **two** existing candidates, they are one person → repoint `identities`, `raw_source_rows`, `field_provenance`, `change_log`, `enrichment_cache` from loser Y to survivor X, then delete Y — **all in one transaction, repoint-before-delete** (never rely on `ON DELETE CASCADE`, which would destroy Y's data). CASCADE stays only as an orphan safety net.
- **Survivor rule (deterministic):** `MIN(first_seen_at)`, tie-broken by `MIN(candidate_id)` (ULIDs sort lexicographically). Survivor keeps `min(first_seen_at)`, `max(last_seen_at)`. Global `UNIQUE(identity_type, match_key)` guarantees the repoint can't hit a unique violation.

---

## Indexes (final set, one line each)
| Index | Why |
|---|---|
| `raw_source_rows(content_hash)` UNIQUE | **the** idempotency anchor; covers `_row`-absent case |
| `raw_source_rows(candidate_id)` | merge repoint + "all raw rows for this person" |
| `raw_source_rows(source, source_row_id)` partial UNIQUE | natural-key backstop where `_row` exists |
| `candidates(normalized_name)` btree | exact/prefix name for the name+company soft-merge rule |
| `candidates(normalized_name)` GIN trgm | `q=` fuzzy/substring over name |
| `candidates(current_title)` GIN trgm | `q=` substring over title |
| `candidates(skills)` GIN | `role=`/skill containment search |
| `candidates(location_city)` | `location=` filter |
| `candidates(experience_months)` | `min_experience` range |
| `candidates(open_to_work) WHERE open_to_work` | **partial** — boolean has no selectivity as a full index |
| `identities(identity_type, match_key)` UNIQUE | hot ingest probe + merge-correctness backbone |
| `identities(candidate_id)` | GET /{id} identities + repoint |
| `field_provenance(candidate_id, field_name) WHERE is_current` **UNIQUE** | one-query current read + single-current invariant |
| `field_provenance(candidate_id, field_name, observed_at DESC)` | ordered superseded history |
| `change_log(candidate_id, applied_at DESC NULLS LAST)` | GET /{id} change history |
| `enrichment_cache(ttl_expires_at)` | negative-cache TTL eviction sweep |
| `run_reports(batch, started_at DESC)` | `/stats?batch=` + latest |
| `pg_trgm` extension | required by the two GIN trgm indexes |

Multi-optional-filter list queries are served by **bitmap-AND across these single-column indexes** — no composite for 5 optional predicates.

## "Why does this row say this?" — one query each
```sql
-- current value + attribution for every field
SELECT field_name, value, source, source_row_id, observed_at, confidence, note
FROM field_provenance WHERE candidate_id = $1 AND is_current ORDER BY field_name;

-- full change history, newest first
SELECT event_id, field_name, old_value, new_value, source, source_row_id,
       batch, rule, note, applied_at
FROM change_log WHERE candidate_id = $1
ORDER BY applied_at DESC NULLS LAST, event_id;
```

## Contract → schema coverage (Appendix A) — every column has a home
- Canonical record (A.2): scalar cols on `candidates`; `location{}` from `location_*`; `emails[]`/`phones[]` derived from `identities` (type/value/`email_type`/`confidence`/source-from-`observed_in`); `skills` array; metrics from maintained cols; `data_quality_flags` array.
- `identities[]` (A.2): direct, incl. `canonical`/`raw_forms`/`observed_in`.
- `field_provenance` incl. `superseded[].reason` (A.2): direct.
- `change_log` incl. rejected null case (A.2): direct.
- CSV (A.3): `primary_email`/`primary_phone` = `is_canonical` pick (prefer work) from `identities`; `enriched`/`enrichment_cost_inr` from `enrichment_cache` (**sum cost if billed under both handle+email**); rest direct.

---

## Verification (Part 1 done = migration applies clean)
1. `docker compose up -d db` (Postgres 16), then `psql < migrations/001_init.sql` → applies with **zero errors**, `pg_trgm` created first.
2. `\d+` on each of the 7 tables — confirm every column above exists, the two `UNIQUE` invariant indexes are `UNIQUE`, and CHECK/FK constraints are present.
3. Re-run the migration (it is `IF NOT EXISTS` throughout) → idempotent, no errors.
4. Sanity insert: one candidate + one identity + a second identity with a duplicate `(identity_type, match_key)` → **rejected by UNIQUE** (proves the resolver backbone). Two `is_current` rows for one field → **rejected** (proves the invariant). Clean up after.

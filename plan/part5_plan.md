# Part 5 Plan — Serve it (FastAPI) + Docker + the enriched CSV deliverable

## Context

Parts 1-4 built a trustworthy, enriched candidate table inside Postgres. Part 5
makes it *usable and reproducible*: a small FastAPI service to search/inspect
it, a Docker setup so a grader can `docker compose up` and hit it, and the
actual deliverable — `out/candidates_enriched.csv`, "the thing that did not
exist when you started."

The brief's own words set the bar: *"We will run `docker compose up`, then your
ingest command, twice. If the second run changes the table or spends money,
that is the first thing we will find."* So Part 5 is graded mostly on judging
criteria 1 (clean-clone run via README), 2 (double-run idempotency of the whole
pipeline, now including the CSV), and 6 (reviewable code). Everything the API
and CSV need is **already precomputed** on `candidates`/`identities` by
`finalize_metrics`/`finalize_canonical_identities` — Part 5 reads, it does not
recompute.

## Confirmed decisions

- **`enrichment_cost_inr` = actual per-candidate spend** — `SUM(cost_inr)` over
  the candidate's `enrichment_cache` rows (found *and* not_found). A paid miss
  (Nikhil: not_found, ₹0.09) shows `enriched=false, enrichment_cost_inr=0.09` —
  honest accountability of spend. `enriched` is a separate boolean = the
  candidate has a `status='found'` enrichment_cache row that was merged.
- **Add `scripts/run_pipeline.sh`** (ingest batch_01 + batch_02 + enrich +
  export) as a convenience wrapper, AND document every step individually in
  the README so the grader can still run ingest twice by hand.

## Deliverable 1 — CSV exporter: `src/export.py` (CLI: `python -m src.export`)

Writes `out/candidates_enriched.csv` using stdlib `csv`. Deterministic:
`ORDER BY candidate_id` (ULIDs sort chronologically) so re-running produces a
byte-identical file. Exact 22-column order from Appendix A.3:

```
candidate_id, full_name, current_title, current_company, city, country,
experience_months, annual_salary_inr, notice_period_days, skills,
open_to_work, primary_email, primary_phone, contactable,
completeness_score, source_count, identity_count, first_seen_at,
last_seen_at, enriched, enrichment_cost_inr, data_quality_flags
```

Single SQL query joining precomputed columns + canonical identities + an
enrichment aggregate (LEFT JOINs / correlated subqueries), e.g.:

- `city` = `location_city`, `country` = `location_country` (flattened from the
  nested location; API keeps it nested, CSV flattens — per A.3).
- `skills` → semicolon-joined in stored array order: `"python;fastapi;..."`.
- `primary_email` / `primary_phone` → `identities.value WHERE is_canonical AND
  identity_type='email'|'phone'` (guaranteed exactly one each by
  `finalize_canonical_identities`); empty string when none.
- `enriched` → `EXISTS (enrichment_cache ec WHERE ec.candidate_id=c.candidate_id
  AND ec.status='found' AND ec.merged_at IS NOT NULL)`.
- `enrichment_cost_inr` → `COALESCE(SUM(ec.cost_inr), 0.00)` per candidate,
  formatted `%.2f`.
- `data_quality_flags` → semicolon-joined array (e.g. `name_collision_reviewed`).
- Booleans (`open_to_work`, `contactable`, `enriched`) rendered lowercase
  `true`/`false`; `open_to_work` NULL → `false` (only value the A.3 example
  shows; documented in DECISIONS.md).
- `completeness_score` as the stored 3-dp numeric; timestamps as ISO8601.

`main(argv)` mirrors `ingest.py`/`enrich.py`: `connect` → `ensure_schema` →
write CSV → print the row count + path → close.

## Deliverable 2 — FastAPI service: `src/api.py` (uvicorn `src.api:app`)

One module, three endpoints, raw SQL via a short-lived `db.connect()` per
request (simple + correct at this scale; a pool is noted as future work in
INFRA.md). `ensure_schema` on startup. Reuses `config.DB_DSN` so the same
`DATABASE_URL` env var works in-container and on-host.

- **`GET /candidates`** — filters `q`, `role`, `location`, `open_to_work`,
  `min_experience` (+ pagination `limit`/`offset`, defaults 50/0, `limit`
  capped at 200). Built as a parameterized WHERE (never string-interpolated):
  - `q` → case-insensitive match on `normalized_name` OR `current_title`
    (`ILIKE %q%`).
  - `role` → `current_title ILIKE %role%` OR `role = ANY(skills)`.
  - `location` → `location_city ILIKE %location%` (so "Bangalore"/"bengaluru"
    both hit; city is normalized to `bengaluru` in the table).
  - `open_to_work` → boolean filter when provided.
  - `min_experience` → **years in the API**, compared as
    `experience_months >= min_experience*12` (brief's param reads as years;
    column is months — conversion documented).
  - Returns `{total, limit, offset, results:[<candidate summary>...]}` ordered
    by `completeness_score DESC, candidate_id ASC` (best-formed first, stable).
- **`GET /candidates/{id}`** — the debug endpoint ("why does this row say
  this"). Returns the canonical record (location as nested
  `{raw,city,state,country}`, `emails[]{value,type,source,confidence}`,
  `phones[]{value,source,confidence}`) PLUS three sibling sections per
  Appendix A.2: `identities[]` (`{type,value,canonical,observed_in,first_seen,
  raw_forms}`), `field_provenance` (object keyed by field, each with current
  value/source/observed_at/confidence/note + a `superseded[]` list built from
  the non-current `field_provenance` rows), and `change_log[]`
  (`{event_id,field,old_value,new_value,source,source_row,batch,applied_at,
  rule,note}`, including REJECTED rows with `applied_at=null`). 404 if unknown.
- **`GET /stats`** — reads the latest `run_reports` row (`ORDER BY started_at
  DESC LIMIT 1`), optional `?batch=` filter (uses `idx_run_reports_batch`).
  Returns the stored `report` JSONB as-is.

Response shaping helpers kept small and local; SQL only touches existing tables
and indexes — no schema change needed.

## Deliverable 3 — Docker

- **`Dockerfile`** (repo root): `python:3.12-slim`, `WORKDIR /app`, copy
  `requirements.txt` → `pip install --no-cache-dir -r requirements.txt`, copy
  the project, default `CMD` runs uvicorn: `uvicorn src.api:app --host 0.0.0.0
  --port 8000`.
- **`docker-compose.yml`** (repo root): two services —
  - `db`: `postgres:16`, env `POSTGRES_USER/PASSWORD/DB=saral`, named volume
    `saral_pgdata:/var/lib/postgresql/data` (persistence), healthcheck
    `pg_isready -U saral`, host port `55432:5432` (matches the existing
    `saral_pg` mapping so nothing on the host collides).
  - `api`: build `.`, `depends_on: db (condition: service_healthy)`,
    `environment: DATABASE_URL=postgresql://saral:saral@db:5432/saral` (service
    hostname + INTERNAL port — not 55432), `ports: 8000:8000`, mounts the repo
    (or at least `./out` and `./data`) so generated `out/candidates_enriched.csv`
    lands on the host. `command` runs the API; ingest/enrich/export are run via
    `docker compose exec api ...`.
- The API container reaches Postgres at `db:5432`; host tools use
  `localhost:55432`. This host-vs-container DSN split is the one thing to get
  right and is called out in the README.

## Deliverable 4 — `scripts/run_pipeline.sh`

`set -euo pipefail`; runs `python -m src.ingest batch_01`, `... batch_02`,
`python -m src.enrich`, `python -m src.export` in order; echoes progress. Run
inside the api container. Individual commands stay documented in the README.

## Deliverable 5 — `README.md` (repo root)

Copy-pasteable, in order: prerequisites; `docker compose up -d --build`; the
one-shot `docker compose exec api bash scripts/run_pipeline.sh`; the equivalent
step-by-step commands; the explicit **double-run idempotency demo** (run the
pipeline twice, show run-2 spend ₹0 + table unchanged); the three curl examples
(`/candidates?q=backend&location=bengaluru&open_to_work=true`,
`/candidates/{id}`, `/stats`); where the CSV lands; and the host-vs-container
`DATABASE_URL` note (host port 55432).

## Tests — `tests/test_api_export.py` (new)

Uses FastAPI `TestClient` (needs **`httpx`** — add to `requirements.txt`). Seed
via `ingest_batch` + `run_enrichment` on the `clean_db` fixture, point the app's
connection at the test DB.
- `test_search_filters`: `q`/`location`/`open_to_work`/`min_experience` each
  narrow results correctly (e.g. Bangalore + open_to_work returns expected set,
  excludes the two Aaravs appropriately).
- `test_candidate_detail_has_provenance_and_changelog`: `/candidates/{id}` for
  Vikram returns identities + field_provenance (with a `superseded` entry) +
  change_log; 404 for a bogus id.
- `test_stats_returns_latest_report`: `/stats` returns the most recent
  `run_reports` row.
- `test_csv_is_deterministic_and_correct`: run `export` twice → byte-identical
  file; assert header is the exact 22-column A.3 order; assert Nikhil row shows
  `enriched=false, enrichment_cost_inr=0.09`; a found candidate shows
  `enriched=true`; both Aaravs present with `name_collision_reviewed` in flags;
  `skills` is `;`-joined.

## Files

- NEW `src/export.py`, `src/api.py`
- NEW `Dockerfile`, `docker-compose.yml`, `README.md`, `scripts/run_pipeline.sh`
- NEW `tests/test_api_export.py`
- EDIT `requirements.txt` (+`httpx` for TestClient)
- EDIT `DECISIONS.md` (short §9: CSV column derivations — cost=actual spend,
  open_to_work NULL→false, skills `;`-join, primary_* from canonical identities)
- NEW `plan/part5_plan.md` (copy of this plan, per convention)

## Verification

1. `docker compose up -d --build`; `docker compose exec api bash
   scripts/run_pipeline.sh` → confirm `out/candidates_enriched.csv` appears with
   22 columns, 21 data rows, deterministic order.
2. Re-run the pipeline → CSV byte-identical (`diff`/hash), enrichment spend ₹0,
   `business_digest`-style table state unchanged (Part 3 guarantee still holds).
3. `curl "localhost:8000/candidates?q=backend&location=bengaluru&open_to_work=true"`
   → sensible filtered JSON with pagination envelope.
4. `curl localhost:8000/candidates/<a-real-id>` → canonical record + identities
   + field_provenance (with a superseded entry for a conflicted field) +
   change_log (incl. a REJECTED null row); `curl .../<bogus>` → 404.
5. `curl localhost:8000/stats` → latest run report JSON.
6. `pytest tests/` — full suite (12 existing + new API/CSV tests) green.
7. Spot-check the CSV: Nikhil `enriched=false`/`0.09`; a found person
   `enriched=true`; both Aaravs flagged `name_collision_reviewed`.
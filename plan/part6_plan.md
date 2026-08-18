# Part 6 Plan — INFRA.md (one page, five answers)

## Context

Part 6 is a **written document**, not code: `INFRA.md`, one page, at the repo
root (sibling to `README.md`/`DECISIONS.md`/`WRITEUP.md`), answering exactly
five sub-questions from the brief — no more, no less:

1. Which AWS services, and why (incumbent stack named: **ECS Fargate, Aurora
   Postgres, S3, ElastiCache Redis, EC2** — free to argue against, but must
   engage with them).
2. Monthly cost, with worked arithmetic across compute/storage/enrichment API
   at a realistic hit rate.
3. A 3 AM partial failure — impact on the idempotency guarantee and on
   enrichment spend specifically.
4. One specific metric that reveals dedup silently broke overnight
   ("Monitoring" is explicitly disallowed as an answer).
5. What breaks first at 10x volume (10M rows/day), and the mitigation.

The judging criterion is verbatim: *"Is the infra thinking concrete. Real
services, real arithmetic."* — vague hand-waving is exactly what's graded
against. The doc also has to make good on **three existing code-level
promises** to point here:
- `src/ingest.py:9` — "One transaction per run (fine at this size; a 1M/day
  build would checkpoint in chunks -- see INFRA.md)."
- `src/normalize.py:18` — "A real system would use a geocoding service (see
  INFRA.md / WRITEUP.md)."
- `migrations/001_init.sql:10` — "built to scale to 1M rows/day (see index
  rationale + INFRA.md)."

DECISIONS.md §7 already owns the combination-merge/skills-ontology/gazetteer
tradeoffs — INFRA.md references them briefly, doesn't re-argue them.

## Confirmed assumption

**Enrichment hit rate: ~5% of daily rows are genuinely new candidates with a
real, callable identifier** (linkedin_handle/email we haven't billed before)
— the other 95% are re-crawls of people already in the table (the whole
point of the dedup layer) or people with no valid key. This anchors the cost
arithmetic: 1,000,000 × 5% = 50,000 calls/day × ₹0.09 = ₹4,500/day ≈
**₹1,35,000/month** for the enrichment line item alone.

## Structure of `INFRA.md`

One page, five headed sections, one per sub-question, each with the concrete
answer up front and the reasoning/numbers right under it — no preamble.

### 1. AWS services (keep the incumbent stack, justify each)

- **ECS Fargate** — runs the ingest/enrich/export jobs as scheduled tasks
  (daily cron via EventBridge) and the FastAPI service as a long-running
  Fargate service behind an ALB. No servers to patch; scales the API
  independently of the batch jobs. Argue for Fargate over Lambda: a 1M-row
  daily batch with per-row DB round-trips will blow past Lambda's 15-minute
  timeout and its ephemeral-connection-count problem against Postgres.
- **Aurora Postgres** — this project's schema is relational by design (FKs,
  `UNIQUE(identity_type, match_key)` as the resolution backbone, partial
  indexes) — nothing here wants a NoSQL rewrite. Aurora specifically (over
  vanilla RDS Postgres) for read replicas (the API's `/candidates` search
  traffic can read from a replica while ingest writes to the primary) and
  faster failover.
  - **Checkpointed ingest at 1M/day** (making good on `ingest.py:9`): replace
    "one transaction per run" with chunked commits (e.g. every 5,000 rows),
    each chunk's high-water mark (`last committed source_row_id` per batch)
    recorded in a small `ingest_checkpoints` table. A crash resumes from the
    last committed chunk, not row 1 — this is *why* Aurora over single-AZ
    RDS: the checkpoint table itself must survive an AZ failure mid-run.
- **S3** — landing zone for raw crawler drops (`batch_NN_raw.jsonl` today is
  a local file; at scale it's an S3 prefix per crawler per day) and the
  archive for `out/candidates_enriched.csv`/run reports. Also the natural
  home for the **real geocoding provider's response cache** (making good on
  `normalize.py:18`): instead of the hand-maintained `CITY_GAZETTEER`, call a
  real geocoder (e.g. AWS Location Service or Google Geocoding API) once per
  distinct raw location string and cache the `{raw_string: {city,state,
  country,lat,lon}}` result in S3 (or a small DynamoDB table) keyed by the
  raw string — same "pay once per key" pattern already built for enrichment
  in `enrichment_cache`.
- **ElastiCache Redis** — NOT for the core identity-resolution writes (those
  need Postgres's `UNIQUE` constraint as the actual correctness guarantee,
  per DECISIONS.md §2 — a cache can't enforce that). Used for: (a) the
  `identities` hot-lookup pattern (`WHERE identity_type=%s AND match_key=%s`)
  as a read-through cache in front of Postgres once daily volume makes that
  index hot enough to matter, and (b) rate-limiting/backoff state for the
  real enrichment provider (see failure-mode section) — Redis `INCR` with
  TTL is the standard pattern for "N calls per minute" against a real vendor.
- **EC2** — not needed as a separate line item; Fargate covers compute. Only
  case for raw EC2: a reserved/spot instance for the nightly batch job if
  Fargate's per-vCPU-second pricing loses to a steady-state reserved
  instance at this volume — covered under cost arithmetic below, argued
  against unless the numbers say otherwise.

### 2. Monthly cost (worked arithmetic)

Rough, sourced, order-of-magnitude — explicitly a "visibly worked wrong
number beats an unsourced right one":

- **Compute (Fargate)**: nightly batch job, assume ~2 vCPU / 4GB, running
  ~2 hours/day for 1M rows (current pipeline processes ~40 rows/sec
  single-threaded per the batch_02 timing in `out/run_report_batch02.json`;
  1M rows chunked/parallelized across a handful of tasks lands comfortably
  under 2h). At ~$0.04/vCPU-hr + $0.004/GB-hr (us-east-1 Fargate on-demand
  ballpark) → (2×0.04 + 4×0.004) × 2h × 30 days ≈ **$5.8/month** for the
  batch job. API service: 1 always-on small Fargate task (0.5 vCPU/1GB) ≈
  (0.5×0.04+1×0.004)×24×30 ≈ **$17/month**.
- **Storage (Aurora)**: 1M rows/day × ~2KB/row raw JSON + candidate/identity/
  provenance/change_log rows ≈ 3-5KB fully-loaded per row → ~4GB/day, ~120GB/
  month growth. Aurora storage ≈ $0.10/GB-month → **~$12-35/month** growing
  monthly (before any retention/archival policy — see 10x section).
- **S3**: raw JSONL landing + CSV/report archive, cheap at this volume,
  **~$3-5/month** (Standard tier, no meaningful egress).
- **Enrichment API**: per the confirmed hit-rate assumption, 1,000,000 ×
  5% = 50,000 calls/day × ₹0.09 ≈ ₹4,500/day ≈ **₹1,35,000/month**
  (~$1,600/month at ~₹84/$) — **by far the largest line item**, which is the
  point: infra cost is a rounding error next to a per-call paid API at this
  volume, and it's exactly why the budget/cache discipline built in Part 4
  matters more at scale, not less.
- **Total**: roughly **$25-30/month AWS infra + ~$1,600/month enrichment** —
  stated as an order-of-magnitude estimate, not a quote.

### 3. 3 AM partial failure

- **What happens**: the checkpointed-chunk design (section 1) means the next
  scheduled run (or an alarm-triggered retry) resumes from the last
  committed chunk. Rows already committed are untouched; the crashed chunk
  and everything after it re-processes from its jsonl/S3 offset.
- **Idempotency guarantee**: **holds** — this is the entire point of
  Part 3's value-based merge (DECISIONS.md §3/§5): re-processing an
  already-committed row a second time is a true no-op (identical value →
  write nothing), and `raw_source_rows.content_hash UNIQUE` plus
  `change_log`'s `UNIQUE(candidate_id, field_name, source_row_id, rule)`
  make double-processing safe even without the chunk checkpoint. The
  checkpoint is purely a *performance* optimization (skip re-reading rows
  we know already committed) — correctness doesn't depend on it.
- **Enrichment spend**: **protected, not free** — Part 4's billing/merging
  split (`src/enrich.py`, `enrichment_cache` + `merged_at`) means a crash
  between billing a key and merging its result never re-bills that key on
  retry (billing is gated on "does a cache row exist," full stop) — but any
  candidate whose *merge* didn't finish before the crash gets caught up on
  retry via the unconditional `WHERE merged_at IS NULL` pass, at zero
  additional cost. The one real risk at 1M/day scale: a crash *during* a
  live HTTP call to a real provider, before the response is durably
  written — mitigated by writing the `enrichment_cache` row (even
  provisionally) before returning success, i.e. treat "billed" as true the
  moment the provider's HTTP response is received, not after our own
  merge logic runs.

### 4. The one metric for "dedup silently broke last night"

**`entities_created / rows_in` ratio, alerted on a sudden jump.** Concretely:
`(entities_created_today / rows_in_today)` compared against a 7-day rolling
median with an alert if it exceeds ~2-3x that baseline. Reasoning: dedup
breaking silently means rows that *should* attach to an existing candidate
instead create a new one — this ratio is the direct, per-run signal already
sitting in every `run_report_*.json` this pipeline emits (`entities_created`
vs `rows_in`, per `src/report.py`). It requires no new instrumentation, just
an alarm on a number the pipeline already produces every run. (Contrast:
"monitoring" or "check the logs" is explicitly disallowed — this is one
named metric, one named threshold.)

### 5. What breaks first at 10x volume (10M rows/day)

**The per-row synchronous SQL round-trip pattern in `identity.process_record`
breaks first** — not the schema, not the API. At 1M/day the pipeline does one
`SELECT` per identifier per row (`_match_candidates`) plus the combination-
match query (a full `WHERE normalized_name=%s` scan, unindexed beyond the
name) — fine at low six-figure daily volume, but at 10M/day the combination-
match fallback path (only hit on zero strong-identifier matches, but still)
and the sheer row-by-row cursor overhead become the bottleneck well before
Aurora's storage or Fargate's compute do. **Mitigation**: batch the strong-
identifier lookups (one `WHERE (identity_type,match_key) IN (...)` per chunk
instead of one query per identifier), and add an index on
`candidates(normalized_name)` if the combination-match path's frequency
grows (currently relies on the implicit btree from no explicit index —
worth confirming/adding explicitly at that volume). Second-order concern:
`raw_source_rows`/`change_log` become genuinely large tables at 10M rows/day
× 365 — partition both by month (`crawled_at`/`observed_at`) before that
becomes a vacuum/query-planning problem, which is the point where "Aurora
Postgres, unpartitioned" would need to become "Aurora Postgres, partitioned"
or a re-argue-the-stack moment.

## Files

- NEW `INFRA.md` (repo root)
- NEW `plan/part6_plan.md` (copy of this plan, per established convention)

No code changes — Part 6 is documentation only.

## Verification

- One page (target ~500-700 words) — check length isn't bloated past what
  the brief asks ("one page").
- Confirm all five sub-questions are answered with a clearly labeled section
  each, in the same order as asked.
- Confirm all three code-level `see INFRA.md` promises (`ingest.py:9`,
  `normalize.py:18`, `migrations/001_init.sql:10`) are each addressed by a
  specific, findable passage (checkpointing, geocoding, index/scale
  rationale respectively) — grep the doc for "checkpoint", "geocod", "1M"
  to self-verify before finishing.
- Confirm the enrichment cost line item uses the confirmed 5% hit-rate
  assumption and shows the multiplication step, not just a final number.
- Confirm the "10x" answer names a specific bottleneck (not a generic "the
  database" answer) and a specific mitigation.
- Confirm the metric answer names exactly one metric + threshold, and does
  not say "monitoring" as the answer.
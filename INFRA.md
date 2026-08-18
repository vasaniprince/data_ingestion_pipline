# INFRA.md — running this for real, at 1M rows/day

## 1. Which AWS services, and why

Keep the incumbent stack — it's the right shape for this problem, not a
default we're stuck with.

- **ECS Fargate** for both the nightly batch (ingest → enrich → export, as
  scheduled EventBridge-triggered tasks) and the FastAPI service (a
  long-running Fargate service behind an ALB). Not Lambda: 1M rows/day with
  a per-row DB round-trip blows past Lambda's 15-minute timeout and its
  connection-count-vs-Postgres problem; Fargate has no such ceiling and lets
  the API scale independently of the batch job.
- **Aurora Postgres**, not a NoSQL rewrite — this schema is relational by
  design (FKs, `UNIQUE(identity_type, match_key)` as the actual
  identity-resolution guarantee, partial indexes). Aurora over vanilla RDS
  for read replicas (search traffic on `/candidates` reads from a replica
  while ingest writes the primary) and faster failover. At 1M/day, `one
  transaction per run` (`src/ingest.py`) becomes chunked commits instead —
  every ~5,000 rows, with the high-water mark (last committed
  `source_row_id` per batch) recorded in a small `ingest_checkpoints` table.
  A crash resumes from the last committed chunk, not row 1; the checkpoint
  table itself needs Aurora's durability to survive an AZ failure mid-run.
- **S3** as the landing zone for raw crawler drops (an S3 prefix per
  crawler per day, replacing the local `batch_NN_raw.jsonl` files) and the
  archive for `candidates_enriched.csv`/run reports. Also the cache for a
  **real geocoding provider** (AWS Location Service or Google Geocoding),
  replacing the hand-maintained `CITY_GAZETTEER` in `src/normalize.py`: call
  it once per distinct raw location string, cache
  `{raw_string: {city,state,country,lat,lon}}` in S3/DynamoDB keyed by the
  string — the same "pay once per key" pattern already built for enrichment.
- **ElastiCache Redis** — not for identity-resolution writes (those need
  Postgres's `UNIQUE` constraint as the real correctness guarantee; a cache
  can't enforce that). Used for a read-through cache in front of the
  `identities(identity_type, match_key)` hot lookup once volume makes that
  index hot, and for rate-limit/backoff counters (`INCR` + TTL) against a
  real enrichment vendor's per-minute call limit.
- **EC2** — not a separate line item. Fargate covers compute; the only case
  for raw EC2 is a reserved instance for the nightly batch if steady-state
  reserved pricing beats Fargate's per-second billing at this volume — the
  arithmetic below doesn't support that switch.

## 2. Monthly cost (worked arithmetic)

- **Compute**: batch job ~2 vCPU/4GB, ~2h/day (current single-threaded
  throughput is ~40 rows/sec per `out/run_report_batch02.json`'s timing; 1M
  rows chunked across a few parallel tasks fits under 2h) at ≈$0.04/vCPU-hr
  + $0.004/GB-hr → (2×0.04 + 4×0.004) × 2h × 30 ≈ **$5.80/month**. API: one
  always-on 0.5vCPU/1GB task → (0.5×0.04 + 1×0.004) × 24 × 30 ≈
  **$17/month**.
- **Storage**: 1M rows/day × ~3-5KB fully-loaded (raw JSON + candidate/
  identity/provenance/change_log rows) ≈ 4GB/day ≈ 120GB/month growth ×
  ≈$0.10/GB-month ≈ **$12-35/month**, compounding monthly with no retention
  policy (see §5).
- **S3**: raw landing + CSV/report archive, negligible traffic ≈
  **$3-5/month**.
- **Enrichment API**: assuming ~5% of 1M daily rows are genuinely new
  candidates with a real, callable identifier we haven't billed before (the
  other 95% are re-crawls of people already resolved, or people with no
  valid key) → 1,000,000 × 5% = 50,000 calls/day × ₹0.09/call = ₹4,500/day
  ≈ **₹1,35,000/month** (~$1,600 at ₹84/$) — **the dominant line item by
  roughly 50×**. Infra cost here is a rounding error next to a per-call
  paid API at this volume, which is exactly why the budget/cache discipline
  built in Part 4 matters more at scale, not less.
- **Total**: order-of-magnitude **~$25-30/month AWS infra + ~$1,600/month
  enrichment**, not a quote.

## 3. It fails at 3 AM, halfway through

Chunked commits (§1) mean the next scheduled run resumes from the last
committed chunk — already-committed rows are untouched; the crashed chunk
and everything after it reprocesses.

- **Idempotency guarantee: holds.** This is Part 3's actual mechanism, not
  the checkpoint: `raw_source_rows.content_hash UNIQUE`,
  `change_log`'s `UNIQUE(candidate_id, field_name, source_row_id, rule)`,
  and the value-based merge's "identical value writes nothing" rule all
  make reprocessing an already-committed row a true no-op with or without a
  checkpoint. The checkpoint is a *performance* optimization (skip
  re-reading known-good rows) — correctness never depends on it.
- **Enrichment spend: protected, not automatically free.** Part 4's split
  between billing (`enrichment_cache` row exists → never re-billed) and
  merging (`merged_at IS NULL` → retried until it succeeds) means a crash
  between billing a key and merging its result costs nothing extra on
  retry — the catch-up pass finds it and merges at zero additional spend.
  The one real exposure at this volume: a crash *during* the live HTTP call
  to a real provider, before we've durably recorded that we were billed —
  mitigated by writing the `enrichment_cache` row the moment the provider's
  HTTP response is received, before any of our own merge logic runs, so
  "billed" is recorded independently of whether the rest of the pipeline
  survives.

## 4. The one metric that catches dedup silently breaking overnight

**`entities_created / rows_in`, alarmed against its own 7-day rolling
median.** If dedup breaks, rows that should attach to an existing candidate
instead create new ones — this ratio moves immediately, and it's a number
every run already emits in `run_report_*.json` (`src/report.py`) with zero
new instrumentation. Alarm if a run's ratio exceeds ~2-3× its trailing
7-day median. ("Monitoring" is not this answer — this is one metric, one
threshold, already sitting in output the pipeline produces every run.)

## 5. What breaks first at 10x volume (10M rows/day)

**The per-row synchronous SQL round-trip in `identity.process_record`** —
not the schema, not Aurora's storage, not the API. Today it issues one
`SELECT` per identifier per row (`_match_candidates`) plus, on the
combination-match fallback, a `WHERE normalized_name=%s` scan. Fine at
1M/day; at 10M/day the row-by-row cursor overhead and that fallback path
become the bottleneck well before Aurora or Fargate compute do.
**Mitigation**: batch strong-identifier lookups into one
`WHERE (identity_type,match_key) IN (...)` per chunk instead of one query
per identifier, and add an explicit index on `candidates(normalized_name)`
if the combination-match path's frequency grows with volume. Second-order:
`raw_source_rows`/`change_log` become genuinely large tables at
10M/day × 365 — partition both by month (`crawled_at`/`observed_at`) before
that becomes a vacuum/planner problem, which is the point "Aurora Postgres,
unpartitioned" needs to become "Aurora Postgres, partitioned."

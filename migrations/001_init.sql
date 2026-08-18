-- ============================================================================
-- SARAL candidate ingest schema  (migration 001)
-- ----------------------------------------------------------------------------
-- Design goals:
--   * one canonical row per real person, created once, updated forever
--   * never throw a raw source row away
--   * every field is traceable to a source ("why does this row say this?"
--     is answerable in ONE query)
--   * idempotent re-ingest: re-running a batch changes nothing and logs nothing
--   * built to scale to 1M rows/day (see index rationale + INFRA.md)
--
-- The migration is fully re-runnable (IF NOT EXISTS throughout).
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- required by the GIN trgm indexes below


-- ---------------------------------------------------------------------------
-- 1. raw_source_rows : every raw row, forever. Provenance + idempotency anchor.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_source_rows (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch            TEXT        NOT NULL,          -- 'batch_01' | 'batch_02' | 'enrichment'
    source           TEXT        NOT NULL,          -- linkedin_scraper | naukri_export | github_crawl | enrichment
    source_row_id    INTEGER,                       -- the _row field, if present
    crawled_at       TIMESTAMPTZ,                   -- the _crawled_at field
    raw_payload      JSONB       NOT NULL,          -- the untouched original row
    content_hash     TEXT        NOT NULL,          -- SHA-256 over canonicalised payload; THE idempotency key
    candidate_id     TEXT,                          -- set after identity resolution; NULL for rejected rows
    rejected         BOOLEAN     NOT NULL DEFAULT FALSE,
    rejection_reason TEXT,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now()  -- audit only; excluded from idempotency comparison
);

-- THE idempotency anchor: an identical re-fed row is skipped (ON CONFLICT DO NOTHING).
-- Content-addressed so it also covers the 1M/day case where _row is absent.
CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_rows_content_hash
    ON raw_source_rows (content_hash);
-- Natural-key backstop where _row exists (batch deliberately NOT in the key,
-- so the same logical row re-fed under a different batch label still dedupes).
CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_rows_source_row
    ON raw_source_rows (source, source_row_id) WHERE source_row_id IS NOT NULL;
-- Merge repoint + "give me every raw row that produced this candidate".
CREATE INDEX IF NOT EXISTS idx_raw_rows_candidate
    ON raw_source_rows (candidate_id);


-- ---------------------------------------------------------------------------
-- 2. candidates : one canonical row per person.
--    source_count / identity_count / completeness_score / contactable are
--    maintained on write so serve + CSV export are O(1), not per-read aggregations.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id       TEXT PRIMARY KEY,            -- 'cand_' + ULID (sortable, matches Appendix)
    full_name          TEXT,
    normalized_name    TEXT,                        -- lowercased, whitespace-collapsed; search + soft-match key
    current_title      TEXT,
    current_company    TEXT,
    location_raw       TEXT,
    location_city      TEXT,
    location_state     TEXT,
    location_country   TEXT,
    experience_months  INTEGER,
    annual_salary_inr  BIGINT,
    notice_period_days INTEGER,
    skills             TEXT[]  NOT NULL DEFAULT '{}',   -- union across sources, stored sorted+deduped, never shrinks
    open_to_work       BOOLEAN,
    data_quality_flags TEXT[]  NOT NULL DEFAULT '{}',   -- e.g. 'name_collision_reviewed'
    source_count       INTEGER NOT NULL DEFAULT 0,      -- maintained
    identity_count     INTEGER NOT NULL DEFAULT 0,      -- maintained
    completeness_score NUMERIC(4,3) NOT NULL DEFAULT 0, -- maintained
    contactable        BOOLEAN NOT NULL DEFAULT FALSE,  -- maintained: has >=1 email or phone identity
    first_seen_at      TIMESTAMPTZ,                     -- min(crawled_at) across sources
    last_seen_at       TIMESTAMPTZ,                     -- max(crawled_at); data-derived, never now()
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()  -- bumped only when a field actually changes
);

CREATE INDEX IF NOT EXISTS idx_candidates_normalized_name
    ON candidates (normalized_name);                         -- exact/prefix name for the name+company soft-merge rule
CREATE INDEX IF NOT EXISTS idx_candidates_name_trgm
    ON candidates USING GIN (normalized_name gin_trgm_ops);  -- q= fuzzy/substring over name
CREATE INDEX IF NOT EXISTS idx_candidates_title_trgm
    ON candidates USING GIN (current_title gin_trgm_ops);    -- q= substring over title
CREATE INDEX IF NOT EXISTS idx_candidates_skills_gin
    ON candidates USING GIN (skills);                        -- role=/skill containment search
CREATE INDEX IF NOT EXISTS idx_candidates_city
    ON candidates (location_city);                           -- location= filter
CREATE INDEX IF NOT EXISTS idx_candidates_experience
    ON candidates (experience_months);                       -- min_experience range filter
CREATE INDEX IF NOT EXISTS idx_candidates_open_to_work
    ON candidates (open_to_work) WHERE open_to_work;         -- partial: boolean has no selectivity as a full index


-- ---------------------------------------------------------------------------
-- 3. identities : "these identifiers are the same human".
--    UNIQUE(identity_type, match_key) is the identity-resolution backbone:
--    one identifier -> at most one candidate, enforced by the DB.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS identities (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    candidate_id  TEXT NOT NULL REFERENCES candidates (candidate_id) ON DELETE CASCADE,
    identity_type TEXT NOT NULL CHECK (identity_type IN ('linkedin_handle','email','phone','github_login')),
    value         TEXT NOT NULL,                 -- canonical DISPLAY value (e.g. aarav.mehta@gmail.com, dots kept)
    match_key     TEXT NOT NULL,                 -- dedup key (gmail dots stripped, plus-tag removed, lowercased)
    raw_forms     TEXT[] NOT NULL DEFAULT '{}',  -- every distinct raw spelling seen
    observed_in   TEXT[] NOT NULL DEFAULT '{}',  -- which sources reported this identifier
    email_type    TEXT CHECK (email_type IN ('work','personal')),  -- emails only; NULL otherwise
    confidence    NUMERIC(4,3),
    is_canonical  BOOLEAN NOT NULL DEFAULT FALSE,-- preferred identifier of its type for this person
    first_seen_at TIMESTAMPTZ NOT NULL,
    UNIQUE (identity_type, match_key)
);

CREATE INDEX IF NOT EXISTS idx_identities_candidate
    ON identities (candidate_id);   -- GET /candidates/{id} identities + merge repoint
-- The UNIQUE(identity_type, match_key) above is also the hot ingest-lookup index
-- (one probe per incoming identifier); no separate index needed for it.


-- ---------------------------------------------------------------------------
-- 4. field_provenance : per-field "why", append-only history.
--    Exactly one is_current row per (candidate, field), enforced by a partial
--    UNIQUE index. Older rows are the superseded[] history.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS field_provenance (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    candidate_id  TEXT NOT NULL REFERENCES candidates (candidate_id) ON DELETE CASCADE,
    field_name    TEXT NOT NULL,
    value         JSONB,
    source        TEXT NOT NULL,
    source_row_id BIGINT REFERENCES raw_source_rows (id),
    observed_at   TIMESTAMPTZ NOT NULL,
    confidence    NUMERIC(4,3),
    note          TEXT,                          -- parse notes, e.g. "parsed from '38 LPA'"
    reason        TEXT,                          -- supersession reason, e.g. 'newer_observation'
    is_current    BOOLEAN NOT NULL DEFAULT TRUE,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now()  -- audit only; excluded from idempotency comparison
);

-- Enforces exactly-one-current-per-field AND serves the one-query current read.
CREATE UNIQUE INDEX IF NOT EXISTS uq_field_prov_current
    ON field_provenance (candidate_id, field_name) WHERE is_current;
-- Ordered per-field history for rendering the superseded[] chain.
CREATE INDEX IF NOT EXISTS idx_field_prov_history
    ON field_provenance (candidate_id, field_name, observed_at DESC);


-- ---------------------------------------------------------------------------
-- 5. change_log : every APPLIED or explicitly-REJECTED decision.
--    A true no-op (incoming value identical to current) writes NOTHING here.
--    applied_at = NULL means the change was rejected, not applied.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS change_log (
    event_id      TEXT PRIMARY KEY,              -- 'chg_' + ULID
    candidate_id  TEXT NOT NULL REFERENCES candidates (candidate_id) ON DELETE CASCADE,
    field_name    TEXT NOT NULL,
    old_value     JSONB,
    new_value     JSONB,
    source        TEXT NOT NULL,
    source_row_id BIGINT REFERENCES raw_source_rows (id),
    batch         TEXT NOT NULL,
    rule          TEXT NOT NULL,                 -- e.g. newer_observation_same_source, fill_null,
                                                 -- source_trust_then_recency, REJECTED:null_from_source_is_not_deletion
    note          TEXT,
    applied_at    TIMESTAMPTZ,                   -- NULL = rejected
    -- Belt-and-braces idempotency guard: the same decision from the same raw
    -- row can never be written twice (the content_hash anchor is the primary
    -- guarantee; this backstops it even if a row is re-processed).
    UNIQUE (candidate_id, field_name, source_row_id, rule)
);

CREATE INDEX IF NOT EXISTS idx_change_log_candidate
    ON change_log (candidate_id, applied_at DESC NULLS LAST);  -- GET /{id} change history, newest first
CREATE INDEX IF NOT EXISTS idx_change_log_batch
    ON change_log (batch);                                     -- run-report aggregation per batch


-- ---------------------------------------------------------------------------
-- 6. enrichment_cache : pay once per key. Negative results cached too, with TTL.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS enrichment_cache (
    cache_key      TEXT PRIMARY KEY,             -- the linkedin handle or email we called with
    status         TEXT NOT NULL,                -- found | not_found
    response       JSONB,
    candidate_id   TEXT REFERENCES candidates (candidate_id) ON DELETE SET NULL,
    cost_inr       NUMERIC(6,2) NOT NULL,
    fetched_at     TIMESTAMPTZ NOT NULL,
    ttl_expires_at TIMESTAMPTZ NOT NULL,         -- negative cache expiry; positive results set far future
    merged_at      TIMESTAMPTZ                   -- NULL until a 'found' row's fields have been
                                                  -- written onto the candidate; decouples billing
                                                  -- (never re-billed once this row exists) from
                                                  -- merging (retried every run until it succeeds)
);

ALTER TABLE enrichment_cache ADD COLUMN IF NOT EXISTS merged_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_enrichment_cache_expiry
    ON enrichment_cache (ttl_expires_at);        -- TTL eviction sweep for expired negative entries

CREATE INDEX IF NOT EXISTS idx_enrichment_cache_unmerged
    ON enrichment_cache (status) WHERE merged_at IS NULL;  -- catch-up merge pass


-- ---------------------------------------------------------------------------
-- 7. run_reports : one row per pipeline run; GET /stats reads the latest.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_reports (
    run_id      TEXT PRIMARY KEY,                -- 'run_' + ULID
    batch       TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    report      JSONB NOT NULL                   -- full Appendix A.4 run report
);

CREATE INDEX IF NOT EXISTS idx_run_reports_batch
    ON run_reports (batch, started_at DESC);     -- /stats?batch= and latest-report lookups

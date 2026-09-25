-- 234_retailer_ingest_pipeline.sql
-- An unattended, self-verifying ingest lane for (brand, retailer) cohorts.
--
-- WHY A NEW PAIR OF TABLES, not catalog_onboard_queue (158): that queue's worker applies
-- unconditionally (jobs/catalog_onboard_job.py passes apply=True), its status CHECK has no
-- "held for review" or "apply due" state, and its accessors swallow errors at debug level. This
-- lane needs every run recorded and a store held when an automated check flags a row -- measured
-- 2026-09-23: k-touch.us types a face tone-up cream as `LIP TINT`, and only reading the dry run's
-- rows caught it.
--
-- retailer_ingest_jobs  one row per (brand, host, scope): the state machine.
--   queued       -> a dry run is due at next_run_at
--   apply_due    -> the last dry run passed every check; an apply is due at next_run_at
--   held         -> a check flagged the cohort; waits for approve (with exclusions) or cancel
--   done         -> applied and verified by readback
--   nothing      -> the cohort resolves to zero products under its filters (closed, not failed)
--   failed       -> gave up (retry budget spent, capped crawl, verification mismatch): see the run
--   cancelled    -> an operator closed it
-- retailer_ingest_runs  the ledger: one row per dry run / apply, with every check's result.
--
-- Idempotent. Prod does not self-apply numbered migrations: apply by one-off job with
-- db.sql_migrations.split_statements, then verify the tables exist.
BEGIN;

CREATE TABLE IF NOT EXISTS retailer_ingest_jobs (
    id              TEXT PRIMARY KEY,
    domain          TEXT NOT NULL,
    brand           TEXT NOT NULL,
    options         JSONB NOT NULL DEFAULT '{}'::jsonb,
    scope_key       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'apply_due', 'held', 'done', 'nothing',
                                      'failed', 'cancelled')),
    priority        INTEGER NOT NULL DEFAULT 0,
    next_run_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 6,
    lease_until     TIMESTAMPTZ,
    last_run_id     TEXT,
    status_reason   TEXT,
    source          TEXT,
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One open job per (host, brand, scope): re-enqueueing the same cohort is a no-op.
CREATE UNIQUE INDEX IF NOT EXISTS ux_retailer_ingest_jobs_open
  ON retailer_ingest_jobs (scope_key)
  WHERE status IN ('queued', 'apply_due', 'held');

-- Claim path: due work, highest priority first, then oldest.
CREATE INDEX IF NOT EXISTS idx_retailer_ingest_jobs_due
  ON retailer_ingest_jobs (priority DESC, next_run_at)
  WHERE status IN ('queued', 'apply_due');

CREATE TABLE IF NOT EXISTS retailer_ingest_runs (
    id              TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES retailer_ingest_jobs (id),
    stage           TEXT NOT NULL CHECK (stage IN ('dry_run', 'apply')),
    outcome         TEXT,
    image_sha       TEXT,
    execution       TEXT,
    crawl           JSONB,
    plan            JSONB,
    checks          JSONB,
    flags           JSONB,
    applied         JSONB,
    readback        JSONB,
    error           TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_retailer_ingest_runs_job
  ON retailer_ingest_runs (job_id, started_at DESC);

COMMIT;

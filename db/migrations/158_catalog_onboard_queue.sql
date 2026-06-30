-- 158_catalog_onboard_queue.sql
-- Unattended catalog-coverage growth: a durable queue of brands/candidates to
-- onboard into the commerce index, drained by a worker on a schedule. Sources
-- enqueue work (curated brand lists; audit competitor-discovery, recurrence-
-- prioritized); the worker runs the existing feeds (curated_brand_feed /
-- catalog_enrichment_agent.runner) + ingest, so growth doesn't need a human to
-- run a CLI per brand.
--
-- One row per (kind, dedup_key) while pending/processing (partial unique index)
-- so re-enqueues are idempotent. Claimed with FOR UPDATE SKIP LOCKED, highest
-- priority first (priority = cross-audit recurrence rank).
--
-- Idempotent. Prod skips the migration runner — apply via railway ssh / admin.
BEGIN;

CREATE TABLE IF NOT EXISTS catalog_onboard_queue (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('curated_brand', 'audit_candidate')),
    dedup_key     TEXT NOT NULL,           -- idempotent-enqueue key (e.g. domain, or normalized brand+product)
    payload       JSONB NOT NULL,          -- curated_brand: {domain, category_path, brand?}; audit_candidate: a Path-C candidate
    priority      INTEGER NOT NULL DEFAULT 0,   -- higher drains first (recurrence rank)
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'processing', 'done', 'failed', 'skipped')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    source        TEXT,                    -- 'curated_list' | 'audit:<run_id>' | ...
    result_jsonb  JSONB,                   -- worker summary (ingest counts)
    error         TEXT,
    claimed_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Idempotent enqueue: one live (pending/processing) row per (kind, dedup_key).
CREATE UNIQUE INDEX IF NOT EXISTS ux_catalog_onboard_queue_live
  ON catalog_onboard_queue (kind, dedup_key)
  WHERE status IN ('pending', 'processing');

-- Claim path: pending, highest priority, oldest first.
CREATE INDEX IF NOT EXISTS idx_catalog_onboard_queue_claim
  ON catalog_onboard_queue (priority DESC, created_at)
  WHERE status = 'pending';

-- Lease reaper / observability.
CREATE INDEX IF NOT EXISTS idx_catalog_onboard_queue_processing
  ON catalog_onboard_queue (claimed_at)
  WHERE status = 'processing';

COMMIT;

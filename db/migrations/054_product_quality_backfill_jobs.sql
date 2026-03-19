CREATE TABLE IF NOT EXISTS product_quality_backfill_jobs (
  job_id VARCHAR(64) PRIMARY KEY,
  merchant_id VARCHAR(100) NOT NULL,
  platform VARCHAR(50),
  status VARCHAR(32) NOT NULL DEFAULT 'queued',
  requested_by VARCHAR(255),
  requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  total_candidates INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  force_refresh BOOLEAN NOT NULL DEFAULT FALSE,
  missing_only BOOLEAN NOT NULL DEFAULT TRUE,
  errors_sample JSONB
);

CREATE INDEX IF NOT EXISTS idx_quality_backfill_jobs_merchant_status_requested
  ON product_quality_backfill_jobs(merchant_id, status, requested_at);

-- Migration 033: Quote-first (pricing lock) - formalize `quotes` table
-- Purpose:
-- - Make quote preview durable and queryable (no best-effort DDL).
-- - Support safe retries via consumed_order_id linkage.
-- Safe to run multiple times.

CREATE TABLE IF NOT EXISTS quotes (
  quote_id VARCHAR(64) PRIMARY KEY,
  merchant_id VARCHAR(64) NOT NULL,
  agent_id VARCHAR(64),
  engine VARCHAR(64) NOT NULL,
  engine_ref VARCHAR(256) NOT NULL,
  request_fingerprint VARCHAR(128) NOT NULL,
  request_json JSONB NOT NULL,
  snapshot_json JSONB NOT NULL,
  quote_hash_sha256 CHAR(64),
  status VARCHAR(32) NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ,
  consumed_order_id VARCHAR(64),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  debug_id VARCHAR(64),
  notes TEXT
);

-- Forward-safe: if the table was created by best-effort DDL, add missing columns.
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS quote_hash_sha256 CHAR(64);
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS consumed_order_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_quotes_merchant_id ON quotes(merchant_id);
CREATE INDEX IF NOT EXISTS idx_quotes_status ON quotes(status);
CREATE INDEX IF NOT EXISTS idx_quotes_expires_at ON quotes(expires_at);
CREATE INDEX IF NOT EXISTS idx_quotes_request_fingerprint ON quotes(request_fingerprint);
CREATE INDEX IF NOT EXISTS idx_quotes_consumed_order_id ON quotes(consumed_order_id);


-- Migration 034: PCS v0.2-b reducer minimal (facts -> current state)
-- Goals:
-- - Append-only fact store with strict dedupe.
-- - Deterministic, replayable reducer output stored in pcs_orders_current.
-- - Per-merchant checkpoints to support incremental replay without missing out-of-order facts.
--
-- Safe to run multiple times.

-- Many existing migrations rely on gen_random_uuid(); ensure pgcrypto exists.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1) Append-only facts (Shopify webhooks + internal facts).
CREATE TABLE IF NOT EXISTS pcs_order_facts (
  id            BIGSERIAL PRIMARY KEY,
  merchant_id   VARCHAR(50) NOT NULL,
  stream_id     TEXT NOT NULL DEFAULT 'orders',
  order_id      TEXT,
  fact_id       UUID NOT NULL DEFAULT gen_random_uuid(),
  fact_type     TEXT NOT NULL,
  occurred_at   TIMESTAMPTZ,
  received_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  source        TEXT NOT NULL CHECK (source IN ('shopify_webhook','internal')),
  topic         TEXT,
  source_event_id TEXT,
  dedupe_key    TEXT NOT NULL,
  payload_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload_sha256 CHAR(64),
  chain_hash    CHAR(64),
  UNIQUE (merchant_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_pcs_order_facts_merchant_occurred
  ON pcs_order_facts(merchant_id, occurred_at ASC, id ASC);

CREATE INDEX IF NOT EXISTS idx_pcs_order_facts_merchant_received
  ON pcs_order_facts(merchant_id, received_at ASC, id ASC);

CREATE INDEX IF NOT EXISTS idx_pcs_order_facts_order
  ON pcs_order_facts(merchant_id, order_id, occurred_at ASC, received_at ASC);

CREATE INDEX IF NOT EXISTS idx_pcs_order_facts_stream_received
  ON pcs_order_facts(merchant_id, stream_id, received_at ASC, id ASC);

-- 2) Derived current state (deterministic output).
CREATE TABLE IF NOT EXISTS pcs_orders_current (
  merchant_id            VARCHAR(50) NOT NULL,
  order_id               TEXT NOT NULL,
  current_state_json     JSONB NOT NULL,
  last_fact_occurred_at  TIMESTAMPTZ,
  last_reduced_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_dedupe_key        TEXT,
  PRIMARY KEY (merchant_id, order_id)
);

CREATE INDEX IF NOT EXISTS idx_pcs_orders_current_order
  ON pcs_orders_current(merchant_id, order_id);

CREATE INDEX IF NOT EXISTS idx_pcs_orders_current_reduced
  ON pcs_orders_current(merchant_id, last_reduced_at DESC);

-- 3) Reducer checkpoints (incremental replay cursor).
CREATE TABLE IF NOT EXISTS pcs_reducer_checkpoints (
  merchant_id     VARCHAR(50) NOT NULL,
  stream_id       TEXT NOT NULL,
  reducer_name    TEXT NOT NULL,
  checkpoint_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (merchant_id, stream_id, reducer_name)
);

CREATE INDEX IF NOT EXISTS idx_pcs_reducer_checkpoints_lookup
  ON pcs_reducer_checkpoints(merchant_id, stream_id, reducer_name);


-- Disputes (PSP + platform) and Returns (platform) records.
-- Minimal schema for v0.2 hardening: store immutable-ish snapshots + normalized status for ops views.

CREATE TABLE IF NOT EXISTS dispute_records (
  id                  BIGSERIAL PRIMARY KEY,
  merchant_id          TEXT NOT NULL,
  -- Source system of the dispute signal.
  source              TEXT NOT NULL CHECK (source IN ('stripe','shopify')),
  -- External dispute identifier (e.g. Stripe dp_*, Shopify dispute id).
  source_dispute_id    TEXT NOT NULL,

  -- Best-effort linkage to our internal order id (ORD_*). May be NULL if we cannot map.
  order_id             TEXT,
  -- Platform order id (e.g. Shopify order id), if available.
  platform_order_id    TEXT,

  -- PSP refs (Stripe).
  payment_intent_id    TEXT,
  charge_id            TEXT,

  currency             TEXT,
  amount               NUMERIC(12, 2),
  reason               TEXT,

  -- Raw status from source and normalized status.
  status_raw           TEXT,
  status               TEXT NOT NULL DEFAULT 'open',

  evidence_due_by      TIMESTAMPTZ,
  opened_at            TIMESTAMPTZ,
  closed_at            TIMESTAMPTZ,

  raw_payload          JSONB NOT NULL DEFAULT '{}'::jsonb,

  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  UNIQUE (source, source_dispute_id)
);

CREATE INDEX IF NOT EXISTS idx_dispute_records_merchant_status
  ON dispute_records (merchant_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_dispute_records_order
  ON dispute_records (merchant_id, order_id);

CREATE TABLE IF NOT EXISTS return_records (
  id                  BIGSERIAL PRIMARY KEY,
  merchant_id          TEXT NOT NULL,
  -- Source platform of the return.
  source              TEXT NOT NULL CHECK (source IN ('shopify')),
  -- External return identifier (Shopify Return id).
  source_return_id     TEXT NOT NULL,

  order_id             TEXT,
  platform_order_id    TEXT,

  status_raw           TEXT,
  status               TEXT NOT NULL DEFAULT 'open',

  -- Best-effort summary fields.
  refund_status_raw    TEXT,
  items_json           JSONB NOT NULL DEFAULT '[]'::jsonb,

  raw_payload          JSONB NOT NULL DEFAULT '{}'::jsonb,

  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  UNIQUE (source, source_return_id)
);

CREATE INDEX IF NOT EXISTS idx_return_records_merchant_status
  ON return_records (merchant_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_return_records_order
  ON return_records (merchant_id, order_id);


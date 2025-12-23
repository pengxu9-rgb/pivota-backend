-- PCS v0.1 Postgres DDL (Shopify)
-- Goals:
-- - Immutable event log (order_events) with tamper-evident hash chain
-- - Normalized facts tables (products/variants/orders/payments/refunds/returns/disputes)
-- - Settlement + reserve + ledger entries (non-custodial, informational)
-- - Evidence packs + audit logs (hashes + blob pointers)

CREATE EXTENSION IF NOT EXISTS pgcrypto;

----------------------------
-- Merchants
----------------------------

CREATE TABLE IF NOT EXISTS merchants (
  merchant_id          TEXT PRIMARY KEY,
  platform             TEXT NOT NULL CHECK (platform IN ('shopify')),
  shop_domain          TEXT NOT NULL UNIQUE,
  shop_gid             TEXT NOT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS merchant_capabilities (
  merchant_id              TEXT PRIMARY KEY REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  shopify_api_version      TEXT,
  scopes_json              JSONB NOT NULL DEFAULT '{}'::jsonb,
  has_shopify_payments     BOOLEAN NOT NULL DEFAULT false,
  has_returns_api          BOOLEAN NOT NULL DEFAULT false,
  last_checked_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_merchants_platform ON merchants(platform);

----------------------------
-- OPS facts: policies
----------------------------

CREATE TABLE IF NOT EXISTS shop_policies (
  merchant_id        TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  policy_type        TEXT NOT NULL CHECK (policy_type IN ('refund', 'shipping', 'privacy', 'terms')),
  url                TEXT NOT NULL,
  title              TEXT,
  body_html          TEXT,
  updated_at         TIMESTAMPTZ,
  hash_sha256        CHAR(64) NOT NULL,
  fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (merchant_id, policy_type, hash_sha256)
);

CREATE INDEX IF NOT EXISTS idx_shop_policies_latest ON shop_policies(merchant_id, policy_type, fetched_at DESC);

----------------------------
-- OPS facts: products / variants
----------------------------

CREATE TABLE IF NOT EXISTS products (
  product_gid        TEXT PRIMARY KEY,
  merchant_id        TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  product_legacy_id  BIGINT,
  title              TEXT NOT NULL,
  handle             TEXT,
  status             TEXT NOT NULL,
  vendor             TEXT,
  product_type       TEXT,
  tags               TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  pcs_metafields     JSONB NOT NULL DEFAULT '{}'::jsonb,
  raw_json           JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at         TIMESTAMPTZ NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_products_merchant_updated ON products(merchant_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS variants (
  variant_gid          TEXT PRIMARY KEY,
  merchant_id          TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  product_gid          TEXT NOT NULL REFERENCES products(product_gid) ON DELETE CASCADE,
  variant_legacy_id    BIGINT,
  title                TEXT NOT NULL,
  sku                  TEXT,
  barcode              TEXT,
  price_amount         NUMERIC(18,6),
  compare_at_amount    NUMERIC(18,6),
  currency             CHAR(3),
  available_for_sale   BOOLEAN,
  requires_shipping    BOOLEAN NOT NULL DEFAULT true,
  taxable              BOOLEAN,
  tax_code             TEXT,
  tracked              BOOLEAN,
  hs_code              TEXT,
  country_of_origin    CHAR(2),
  province_of_origin   TEXT,
  weight_grams         INTEGER,
  inventory_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
  pcs_metafields       JSONB NOT NULL DEFAULT '{}'::jsonb,
  raw_json             JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at           TIMESTAMPTZ NOT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_variants_merchant_sku ON variants(merchant_id, sku);
CREATE INDEX IF NOT EXISTS idx_variants_product ON variants(product_gid);
CREATE INDEX IF NOT EXISTS idx_variants_merchant_updated ON variants(merchant_id, updated_at DESC);

----------------------------
-- Order facts (current state)
----------------------------

CREATE TABLE IF NOT EXISTS orders (
  order_gid                TEXT PRIMARY KEY,
  merchant_id              TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  order_legacy_id          BIGINT,
  order_name               TEXT NOT NULL,
  placed_at                TIMESTAMPTZ NOT NULL,
  updated_at               TIMESTAMPTZ NOT NULL,
  currency                 CHAR(3) NOT NULL,

  shopify_financial_status   TEXT,
  shopify_fulfillment_status TEXT,
  payment_gateways           TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],

  order_state              TEXT NOT NULL CHECK (order_state IN ('proposed','authorized','placed','shipped','delivered','settled','canceled')),
  payment_state            TEXT NOT NULL CHECK (payment_state IN ('authorized','captured','refunded','voided')),

  subtotal_amount          NUMERIC(18,6),
  discount_total_amount    NUMERIC(18,6),
  shipping_fee_amount      NUMERIC(18,6),
  tax_amount               NUMERIC(18,6),
  total_amount             NUMERIC(18,6),

  canceled_at              TIMESTAMPTZ,
  cancel_reason            TEXT,

  -- PCS order-level metafields (mirrored for fast access)
  policy_disclosure_hash   CHAR(64),
  pivota_mandate_id        TEXT,
  pivota_agent_id          TEXT,
  authorization_audit_ref  TEXT,
  ledger_ref               TEXT,

  pcs_metafields           JSONB NOT NULL DEFAULT '{}'::jsonb,
  raw_json                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_merchant_updated ON orders(merchant_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_merchant_placed ON orders(merchant_id, placed_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_merchant_state ON orders(merchant_id, order_state, payment_state);

----------------------------
-- Immutable events (order_events) + hash chain
----------------------------

CREATE TABLE IF NOT EXISTS order_events (
  event_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  merchant_id        TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  order_gid          TEXT NOT NULL REFERENCES orders(order_gid) ON DELETE CASCADE,

  source             TEXT NOT NULL CHECK (source IN ('shopify_webhook','shopify_poll','external','pivota')),
  topic              TEXT NOT NULL,
  idempotency_key    TEXT NOT NULL,

  occurred_at        TIMESTAMPTZ NOT NULL,
  received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

  payload_json       JSONB NOT NULL,
  payload_sha256     CHAR(64) NOT NULL,

  -- Tamper-evident chain (computed by app):
  -- chain_hash = sha256(prev_chain_hash || payload_sha256 || idempotency_key || occurred_at)
  prev_chain_hash    CHAR(64),
  chain_hash         CHAR(64) NOT NULL,

  raw_webhook_id     TEXT,

  UNIQUE (merchant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_order_events_order_time ON order_events(merchant_id, order_gid, occurred_at);
CREATE INDEX IF NOT EXISTS idx_order_events_topic_time ON order_events(merchant_id, topic, occurred_at);

----------------------------
-- Payments / refunds (facts)
----------------------------

CREATE TABLE IF NOT EXISTS payments (
  transaction_gid        TEXT PRIMARY KEY,
  merchant_id            TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  order_gid              TEXT NOT NULL REFERENCES orders(order_gid) ON DELETE CASCADE,
  kind                   TEXT NOT NULL,
  status                 TEXT,
  gateway                TEXT,
  authorization_code     TEXT,
  parent_transaction_gid TEXT,
  processed_at           TIMESTAMPTZ NOT NULL,
  amount                 NUMERIC(18,6) NOT NULL,
  currency               CHAR(3) NOT NULL,
  raw_json               JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (merchant_id, transaction_gid)
);

CREATE INDEX IF NOT EXISTS idx_payments_order_time ON payments(merchant_id, order_gid, processed_at);
CREATE INDEX IF NOT EXISTS idx_payments_kind ON payments(merchant_id, kind);

CREATE TABLE IF NOT EXISTS refunds (
  refund_gid         TEXT PRIMARY KEY,
  merchant_id        TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  order_gid          TEXT NOT NULL REFERENCES orders(order_gid) ON DELETE CASCADE,
  created_at_shopify TIMESTAMPTZ NOT NULL,
  note               TEXT,
  total_refunded     NUMERIC(18,6) NOT NULL,
  currency           CHAR(3) NOT NULL,
  line_items_json    JSONB NOT NULL DEFAULT '[]'::jsonb,
  transactions_json  JSONB NOT NULL DEFAULT '[]'::jsonb,
  raw_json           JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (merchant_id, refund_gid)
);

CREATE INDEX IF NOT EXISTS idx_refunds_order_time ON refunds(merchant_id, order_gid, created_at_shopify);

----------------------------
-- Returns / disputes (facts + external fallback)
----------------------------

CREATE TABLE IF NOT EXISTS returns (
  rma_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  merchant_id          TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  order_gid            TEXT NOT NULL REFERENCES orders(order_gid) ON DELETE CASCADE,
  shopify_return_gid   TEXT,
  return_state         TEXT NOT NULL CHECK (return_state IN ('rma_created','label_issued','in_transit','received','refunded')),
  reason_code          TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  received_at          TIMESTAMPTZ,
  refunded_at          TIMESTAMPTZ,
  raw_json             JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (merchant_id, shopify_return_gid)
);

CREATE INDEX IF NOT EXISTS idx_returns_order_state ON returns(merchant_id, order_gid, return_state);

CREATE TABLE IF NOT EXISTS disputes (
  dispute_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  merchant_id          TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  order_gid            TEXT NOT NULL REFERENCES orders(order_gid) ON DELETE CASCADE,
  shopify_dispute_gid  TEXT,
  dispute_state        TEXT NOT NULL CHECK (dispute_state IN ('opened','evidence_submitted','won','lost','closed')),
  opened_at            TIMESTAMPTZ NOT NULL,
  evidence_due_by      TIMESTAMPTZ,
  finalized_at         TIMESTAMPTZ,
  reason               TEXT,
  raw_json             JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (merchant_id, shopify_dispute_gid)
);

CREATE INDEX IF NOT EXISTS idx_disputes_order_state ON disputes(merchant_id, order_gid, dispute_state);

----------------------------
-- Settlement facts (Shopify Payments, optional)
----------------------------

CREATE TABLE IF NOT EXISTS settlements (
  payout_gid        TEXT PRIMARY KEY,
  merchant_id       TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  status            TEXT,
  payout_date       DATE,
  currency          CHAR(3),
  raw_json          JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (merchant_id, payout_gid)
);

CREATE INDEX IF NOT EXISTS idx_settlements_merchant_date ON settlements(merchant_id, payout_date DESC);

CREATE TABLE IF NOT EXISTS settlement_balance_transactions (
  balance_tx_gid    TEXT PRIMARY KEY,
  merchant_id       TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  payout_gid        TEXT REFERENCES settlements(payout_gid) ON DELETE SET NULL,
  order_gid         TEXT REFERENCES orders(order_gid) ON DELETE SET NULL,
  type              TEXT NOT NULL,
  created_at_shopify TIMESTAMPTZ NOT NULL,
  amount            NUMERIC(18,6) NOT NULL,
  fee               NUMERIC(18,6),
  net               NUMERIC(18,6),
  currency          CHAR(3) NOT NULL,
  raw_json          JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (merchant_id, balance_tx_gid)
);

CREATE INDEX IF NOT EXISTS idx_balance_tx_merchant_time ON settlement_balance_transactions(merchant_id, created_at_shopify);
CREATE INDEX IF NOT EXISTS idx_balance_tx_payout ON settlement_balance_transactions(payout_gid);

----------------------------
-- Ledger entries (event-sourced, informational; not MoR)
----------------------------

CREATE TABLE IF NOT EXISTS ledger_entries (
  ledger_entry_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  merchant_id         TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  order_gid           TEXT REFERENCES orders(order_gid) ON DELETE SET NULL,
  entry_type          TEXT NOT NULL,
  direction           TEXT NOT NULL CHECK (direction IN ('debit','credit')),
  amount              NUMERIC(18,6) NOT NULL CHECK (amount >= 0),
  currency            CHAR(3) NOT NULL,
  occurred_at         TIMESTAMPTZ NOT NULL,
  source_event_id     UUID REFERENCES order_events(event_id) ON DELETE SET NULL,
  metadata_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (merchant_id, source_event_id, entry_type, direction, amount, currency)
);

CREATE INDEX IF NOT EXISTS idx_ledger_entries_order_time ON ledger_entries(merchant_id, order_gid, occurred_at);

CREATE TABLE IF NOT EXISTS reserves (
  reserve_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  merchant_id         TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  order_gid           TEXT REFERENCES orders(order_gid) ON DELETE SET NULL,
  status              TEXT NOT NULL CHECK (status IN ('held','released','canceled')),
  basis               TEXT NOT NULL CHECK (basis IN ('policy','risk_spike','manual')),
  holdback_rate       NUMERIC(10,6) CHECK (holdback_rate >= 0 AND holdback_rate <= 0.5),
  holdback_days       INTEGER CHECK (holdback_days >= 0 AND holdback_days <= 180),
  amount              NUMERIC(18,6) NOT NULL CHECK (amount >= 0),
  currency            CHAR(3) NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  release_at          TIMESTAMPTZ,
  metadata_json       JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_reserves_merchant_status ON reserves(merchant_id, status, release_at);

----------------------------
-- Evidence packs + audit logs (tamper-evident)
----------------------------

CREATE TABLE IF NOT EXISTS evidence_packs (
  evidence_pack_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  merchant_id         TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  order_gid           TEXT REFERENCES orders(order_gid) ON DELETE SET NULL,
  dispute_id          UUID REFERENCES disputes(dispute_id) ON DELETE SET NULL,
  pack_type           TEXT NOT NULL CHECK (pack_type IN ('order_snapshot','dispute_pack')),
  pack_version        INTEGER NOT NULL CHECK (pack_version >= 1),
  status              TEXT NOT NULL CHECK (status IN ('draft','frozen')),
  generated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  frozen_at           TIMESTAMPTZ,
  manifest_json       JSONB NOT NULL,
  manifest_sha256     CHAR(64) NOT NULL,
  signature           TEXT,
  assets_json         JSONB NOT NULL DEFAULT '[]'::jsonb,
  UNIQUE (merchant_id, pack_type, order_gid, dispute_id, pack_version)
);

CREATE INDEX IF NOT EXISTS idx_evidence_packs_order ON evidence_packs(merchant_id, order_gid, pack_type, pack_version DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_packs_dispute ON evidence_packs(merchant_id, dispute_id, pack_version DESC);

CREATE TABLE IF NOT EXISTS audit_logs (
  audit_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  merchant_id        TEXT NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
  actor_type         TEXT NOT NULL CHECK (actor_type IN ('system','staff','app','merchant','customer')),
  actor_ref          TEXT,
  action             TEXT NOT NULL,
  occurred_at        TIMESTAMPTZ NOT NULL,
  payload_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload_sha256     CHAR(64) NOT NULL,
  prev_chain_hash    CHAR(64),
  chain_hash         CHAR(64) NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_merchant_time ON audit_logs(merchant_id, occurred_at);


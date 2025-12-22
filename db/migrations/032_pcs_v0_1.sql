-- PCS v0.1 foundational tables (Shopify facts + evidence + tamper-evident audit)
-- Safe to run multiple times.

CREATE TABLE IF NOT EXISTS pcs_merchant_capabilities (
  merchant_id            VARCHAR(50) PRIMARY KEY,
  shopify_api_version    VARCHAR(20),
  scopes_json            JSONB NOT NULL DEFAULT '{}'::jsonb,
  has_shopify_payments   BOOLEAN NOT NULL DEFAULT FALSE,
  has_returns_api        BOOLEAN NOT NULL DEFAULT FALSE,
  last_checked_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS pcs_shop_policies (
  id                  BIGSERIAL PRIMARY KEY,
  merchant_id         VARCHAR(50) NOT NULL,
  policy_type         VARCHAR(20) NOT NULL CHECK (policy_type IN ('refund','shipping','privacy','terms')),
  url                 TEXT NOT NULL,
  title               TEXT,
  body_html           TEXT,
  updated_at          TIMESTAMPTZ,
  hash_sha256         CHAR(64) NOT NULL,
  fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (merchant_id, policy_type, hash_sha256)
);

CREATE INDEX IF NOT EXISTS idx_pcs_shop_policies_latest
  ON pcs_shop_policies(merchant_id, policy_type, fetched_at DESC);

-- Raw Shopify webhook events (append-only) with idempotency guard and hash chain.
CREATE TABLE IF NOT EXISTS pcs_shopify_webhook_events (
  id                BIGSERIAL PRIMARY KEY,
  merchant_id       VARCHAR(50) NOT NULL,
  shop_domain       VARCHAR(255),
  topic             VARCHAR(100) NOT NULL,
  webhook_id        VARCHAR(100),
  idempotency_key   TEXT NOT NULL,
  signature_verified BOOLEAN NOT NULL DEFAULT FALSE,
  received_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  occurred_at       TIMESTAMPTZ,
  payload_json      JSONB NOT NULL,
  payload_sha256    CHAR(64) NOT NULL,
  prev_chain_hash   CHAR(64),
  chain_hash        CHAR(64) NOT NULL,
  UNIQUE (merchant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_pcs_shopify_webhook_events_time
  ON pcs_shopify_webhook_events(merchant_id, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_pcs_shopify_webhook_events_topic_time
  ON pcs_shopify_webhook_events(merchant_id, topic, received_at DESC);

-- Evidence packs: store manifest + asset pointers, immutable-by-version (append-only versions).
CREATE TABLE IF NOT EXISTS pcs_evidence_packs (
  id              BIGSERIAL PRIMARY KEY,
  merchant_id     VARCHAR(50) NOT NULL,
  order_id        VARCHAR(50),
  dispute_ref     TEXT,
  pack_type       VARCHAR(30) NOT NULL CHECK (pack_type IN ('order_snapshot','dispute_pack')),
  pack_version    INTEGER NOT NULL CHECK (pack_version >= 1),
  status          VARCHAR(20) NOT NULL CHECK (status IN ('draft','frozen')),
  generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  frozen_at       TIMESTAMPTZ,
  manifest_json   JSONB NOT NULL,
  manifest_sha256 CHAR(64) NOT NULL,
  signature       TEXT,
  assets_json     JSONB NOT NULL DEFAULT '[]'::jsonb,
  UNIQUE (merchant_id, pack_type, order_id, dispute_ref, pack_version)
);

CREATE INDEX IF NOT EXISTS idx_pcs_evidence_packs_order
  ON pcs_evidence_packs(merchant_id, order_id, pack_type, pack_version DESC);

CREATE INDEX IF NOT EXISTS idx_pcs_evidence_packs_dispute
  ON pcs_evidence_packs(merchant_id, dispute_ref, pack_version DESC);

-- Audit logs with tamper-evident hash chain (append-only).
CREATE TABLE IF NOT EXISTS pcs_audit_logs (
  id              BIGSERIAL PRIMARY KEY,
  merchant_id     VARCHAR(50) NOT NULL,
  actor_type      VARCHAR(20) NOT NULL CHECK (actor_type IN ('system','staff','app','merchant','customer')),
  actor_ref       TEXT,
  action          TEXT NOT NULL,
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  payload_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload_sha256  CHAR(64) NOT NULL,
  prev_chain_hash CHAR(64),
  chain_hash      CHAR(64) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pcs_audit_logs_merchant_time
  ON pcs_audit_logs(merchant_id, occurred_at DESC);


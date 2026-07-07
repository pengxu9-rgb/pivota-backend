-- 169_shopify_gdpr_requests.sql
-- Audit trail for Shopify GDPR / data-privacy compliance webhooks
-- (customers/data_request, customers/redact, shop/redact).
--
-- Shopify requires the compliance endpoints to return 200; this table is the
-- durable record that the obligation was actually fulfilled (or flagged for
-- review), not just logged-and-200. The redaction/export handlers in
-- routes/webhook_routes.py write one row per compliance webhook with the
-- resolution (counts of rows redacted, export artifact, or the error).
--
-- shopify_request stores the compliance payload (ids, not bulk PII).
-- status: 'received' | 'completed' | 'needs_review'.
--
-- Idempotent DDL. Prod skips the migration runner — apply via railway ssh /
-- admin (matches migrations 159/166/167/168) or rely on schema_guard's startup
-- self-heal (db/schema_guard.ensure_required_schema_light), which mirrors this.
BEGIN;

CREATE TABLE IF NOT EXISTS shopify_gdpr_requests (
  id              BIGSERIAL PRIMARY KEY,
  merchant_id     TEXT,
  shop_domain     TEXT,
  topic           TEXT NOT NULL,
  shopify_request JSONB,                       -- compliance payload (ids, not bulk PII)
  status          TEXT NOT NULL DEFAULT 'received',  -- received | completed | needs_review
  resolution      JSONB,                       -- counts redacted / export artifact / error
  received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_shopify_gdpr_requests_shop_domain
  ON shopify_gdpr_requests (shop_domain);
CREATE INDEX IF NOT EXISTS idx_shopify_gdpr_requests_merchant
  ON shopify_gdpr_requests (merchant_id);

COMMIT;

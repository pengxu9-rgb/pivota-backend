-- 219: record what a checkout preflight WOULD have decided, before it is allowed to decide it.
--
-- services/checkout_preflight.py asks the merchant whether the thing we are about to sell still
-- exists and is in stock, at the moment of checkout rather than at index time. The audit that
-- motivated it measured 31.1% of index records producing a wrong or unexecutable spec on a
-- 90-SKU live sample (12.3% price mismatch, 11.1% listed-but-out-of-stock, 6.7% dead PDP).
--
-- A fail-closed gate on that number would refuse roughly a third of purchases, so it ships in
-- SHADOW first: it computes the verdict, writes it here, and lets the checkout through. The
-- refusal rate then comes from this table against the CURRENT catalog rather than from a sample
-- taken before the variant-identity backfill, and enforcement is armed on evidence.
--
-- Why a table and not a log line. Cloud Logging drops lines — on 2026-09-08 it dropped a
-- completed backfill's entire report while the job exited 0. A shadow mode whose only output is
-- a log is a measurement that silently under-counts, which is the failure it exists to detect.
--
-- `would_block` is the whole point of the row: it is the decision the ENFORCING gate would have
-- made, recorded while the gate is not enforcing. Reading `outcome` instead would only ever say
-- "allow" in shadow.
CREATE TABLE IF NOT EXISTS checkout_preflight_observations (
  observation_id     VARCHAR(64)  PRIMARY KEY,
  created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  mode               VARCHAR(16)  NOT NULL,
  outcome            VARCHAR(16)  NOT NULL,
  would_block        BOOLEAN      NOT NULL,
  reason             VARCHAR(64)  NOT NULL,
  merchant_id        VARCHAR(64),
  product_key        VARCHAR(255),
  sku_key            VARCHAR(255),
  offer_id           VARCHAR(255),
  live_status        VARCHAR(32),
  in_stock           BOOLEAN,
  price_verified     BOOLEAN      NOT NULL DEFAULT FALSE,
  quoted_price       NUMERIC(12, 2),
  quoted_currency    VARCHAR(16),
  live_price         NUMERIC(12, 2),
  live_currency      VARCHAR(16),
  latency_ms         INTEGER,
  detail             JSONB
);

-- The two questions this table exists to answer: "what would we have refused, and why" over a
-- window, and "what happened to this particular checkout".
CREATE INDEX IF NOT EXISTS idx_checkout_preflight_obs_created_at
  ON checkout_preflight_observations (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_checkout_preflight_obs_would_block
  ON checkout_preflight_observations (would_block, reason, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_checkout_preflight_obs_sku
  ON checkout_preflight_observations (sku_key);

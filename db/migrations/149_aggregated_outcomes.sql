-- 149_aggregated_outcomes.sql
-- The durable-moat substrate: per-merchant and per-product transaction OUTCOMES
-- (rail_transacted, refund_rate, GMV) computed from the decision -> order ->
-- paid/refund loop closed in migration 140 + the webhook funnel-link join. This is
-- the one signal class a competitor cannot replicate.
--
-- Honesty is enforced in the schema's spirit: `refund_rate` is NULL until
-- `min_sample_met` is TRUE, so a return rate is never surfaced from a handful of
-- orders. The aggregator (services/outcome_aggregation_service.py) also creates this
-- table idempotently at runtime, because the prod startup migration runner is
-- skipped (SKIP_HEAVY_STARTUP_INIT) — this file is the canonical record.

CREATE TABLE IF NOT EXISTS aggregated_outcomes (
  subject_type     VARCHAR(16)  NOT NULL,                       -- 'merchant' | 'product'
  subject_key      VARCHAR(128) NOT NULL,                       -- merchant_id | canonical_product_id
  window_key       VARCHAR(16)  NOT NULL DEFAULT 'all_time',    -- 'all_time' | 'trailing_90d'
  transacted_count INTEGER      NOT NULL DEFAULT 0,             -- orders that completed payment (paid + refunded + partial)
  paid_count       INTEGER      NOT NULL DEFAULT 0,
  refunded_count   INTEGER      NOT NULL DEFAULT 0,             -- refunded + partially_refunded
  refund_rate      NUMERIC(5,4),                                -- NULL until min_sample_met
  gmv_cents        BIGINT       NOT NULL DEFAULT 0,
  aov_cents        BIGINT,
  currency         VARCHAR(8),
  sample_size      INTEGER      NOT NULL DEFAULT 0,
  min_sample_met   BOOLEAN      NOT NULL DEFAULT FALSE,
  computed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT aggregated_outcomes_pkey PRIMARY KEY (subject_type, subject_key, window_key)
);

CREATE INDEX IF NOT EXISTS idx_aggregated_outcomes_subject
  ON aggregated_outcomes (subject_type, subject_key);
CREATE INDEX IF NOT EXISTS idx_aggregated_outcomes_computed
  ON aggregated_outcomes (computed_at DESC);

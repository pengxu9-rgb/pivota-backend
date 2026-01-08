-- After-sales Case API (v1)
-- Unifies refund_without_return, partial_refund, and refund_with_return (return label flow).
-- NOTE: intentionally stores minimal, mostly non-PII data; do not store addresses/emails here.

CREATE TABLE IF NOT EXISTS after_sales_cases (
  id                     BIGSERIAL PRIMARY KEY,
  case_id                TEXT NOT NULL UNIQUE,
  order_id               TEXT NOT NULL,
  merchant_id            TEXT NOT NULL,
  agent_id               TEXT NOT NULL,

  -- Identity context (optional). For verified agent-user flows, store normalized issuer:sub.
  agent_user_ref         TEXT,
  -- Legacy anonymous compatibility.
  buyer_ref              TEXT,

  -- case_type: refund | return_refund | support (exchange later)
  case_type              TEXT NOT NULL,
  -- resolution: refund_without_return | refund_with_return
  resolution             TEXT NOT NULL,

  reason_code            TEXT,
  reason_text            TEXT,

  -- Requested refund (optional for full refund). Amount is in order currency by default.
  requested_refund_amount NUMERIC(12, 2),
  currency_order         TEXT,
  currency_charge        TEXT,

  -- Optional structured payloads (non-PII).
  line_items_json        JSONB NOT NULL DEFAULT '[]'::jsonb,
  amount_breakdown_json  JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- Status machine (v1 minimal):
  -- requested -> label_issued (if return) -> refund_processed -> closed
  status                 TEXT NOT NULL DEFAULT 'requested',

  -- Placeholder return label URL flow. (Provider integration can be added later.)
  label_url              TEXT,

  -- Best-effort audit log, append-only.
  audit_log              JSONB NOT NULL DEFAULT '[]'::jsonb,

  -- Best-effort idempotency (agent-scoped).
  idempotency_key        TEXT,

  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_after_sales_cases_order
  ON after_sales_cases (order_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_after_sales_cases_agent
  ON after_sales_cases (agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_after_sales_cases_status
  ON after_sales_cases (status, updated_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS ux_after_sales_cases_agent_idempotency
  ON after_sales_cases (agent_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL AND idempotency_key <> '';


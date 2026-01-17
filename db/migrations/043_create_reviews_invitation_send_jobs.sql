-- Reviews invitation send jobs (delayed email sends)
-- Idempotency: unique per (order_id, status) so an order can have at most one pending/processing/sent job at a time.

CREATE TABLE IF NOT EXISTS reviews_invitation_send_jobs (
  id BIGSERIAL PRIMARY KEY,
  merchant_id VARCHAR(64) NOT NULL,
  order_id VARCHAR(64) NOT NULL,
  send_at TIMESTAMPTZ NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'pending', -- pending|processing|sent|error|canceled
  attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TIMESTAMPTZ NULL,
  sent_at TIMESTAMPTZ NULL,
  sendgrid_message_id TEXT NULL,
  last_error TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reviews_invitation_send_jobs_due
  ON reviews_invitation_send_jobs (status, send_at);

CREATE UNIQUE INDEX IF NOT EXISTS ux_reviews_invitation_send_jobs_order_status
  ON reviews_invitation_send_jobs (order_id, status);


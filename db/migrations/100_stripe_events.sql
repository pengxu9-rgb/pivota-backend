-- 100_stripe_events.sql
-- Stripe webhook idempotency ledger for monetization v1.3.

CREATE TABLE IF NOT EXISTS stripe_events (
  id BIGSERIAL PRIMARY KEY,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_jsonb JSONB NOT NULL,
  processed_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_stripe_events_event_id UNIQUE (event_id),
  CONSTRAINT ck_stripe_events_event_id_nonempty CHECK (event_id <> ''),
  CONSTRAINT ck_stripe_events_event_type_nonempty CHECK (event_type <> ''),
  CONSTRAINT ck_stripe_events_status CHECK (status IN ('pending', 'processed', 'failed', 'ignored'))
);

CREATE INDEX IF NOT EXISTS idx_stripe_events_event_type
  ON stripe_events(event_type);
CREATE INDEX IF NOT EXISTS idx_stripe_events_event_id
  ON stripe_events(event_id);
CREATE INDEX IF NOT EXISTS idx_stripe_events_status
  ON stripe_events(status);
CREATE INDEX IF NOT EXISTS idx_stripe_events_processed_at
  ON stripe_events(processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_stripe_events_received_at
  ON stripe_events(received_at DESC);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_stripe_events_updated_at ON stripe_events;
CREATE TRIGGER trg_stripe_events_updated_at
  BEFORE UPDATE ON stripe_events
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

COMMENT ON TABLE stripe_events IS 'Monetization v1.3: idempotency log for Stripe webhook events';
COMMENT ON COLUMN stripe_events.event_id IS 'Unique Stripe event ID for insert-or-skip webhook processing';
COMMENT ON COLUMN stripe_events.payload_jsonb IS 'Raw Stripe webhook event payload';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_stripe_events_updated_at ON stripe_events;
-- DROP TABLE IF EXISTS stripe_events;

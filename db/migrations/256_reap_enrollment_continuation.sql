-- No backfill of dispatch_tracking_version: historic missing IDs are UNKNOWN.
-- Only new purchase INSERTs establish version 1 before any provider operation.
ALTER TABLE reap_agentic_purchases
    ADD COLUMN IF NOT EXISTS dispatch_tracking_version INTEGER,
    ADD COLUMN IF NOT EXISTS checkout_dispatch_key VARCHAR(64),
    ADD COLUMN IF NOT EXISTS contact_received_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS contact_purged_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS contact_revision INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS reap_checkout_dispatch_events (
    purchase_id VARCHAR(64) NOT NULL,
    dispatch_key VARCHAR(64) NOT NULL,
    event_type VARCHAR(32) NOT NULL CHECK(event_type IN ('started','not_created','observed')),
    quote_id VARCHAR(128) NOT NULL,
    enrollment_id VARCHAR(128) NOT NULL,
    checkout_id VARCHAR(128),
    provider_code VARCHAR(64),
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(purchase_id,dispatch_key,event_type)
);
-- The service has no UPDATE/DELETE path for this append-only journal. Preserve it when
-- changing contact data or closing a purchase; a rollback must never erase dispatch evidence.

CREATE OR REPLACE FUNCTION reap_dispatch_events_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'reap dispatch evidence is append-only'; END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER reap_dispatch_events_immutable
BEFORE UPDATE OR DELETE ON reap_checkout_dispatch_events
FOR EACH ROW EXECUTE FUNCTION reap_dispatch_events_immutable();

-- REAP WEBHOOK EVENTS + the settlement columns on agent_issued_cards.
--
-- This is the reconcile half of the card rail (mint half: migration 201). The issuer tells us
-- what actually happened to the instrument — authorized, declined, settled — and this pair of
-- changes is what lets that report (a) be processed exactly once and (b) land somewhere.
--
-- WHY A DEDUP TABLE AND NOT "handlers are idempotent anyway". Webhook providers redeliver: on
-- timeout, on 5xx, on their own retries. Most of our handlers ARE idempotent, but 'exhausted'
-- transitions and outcome upserts are only idempotent per-state, not per-event — a redelivered
-- auth event after a revoke would happily re-exhaust. Recording the event_id makes redelivery
-- a no-op at the door instead of a per-handler proof obligation.
CREATE TABLE IF NOT EXISTS reap_webhook_events (
    event_id VARCHAR(128) PRIMARY KEY,
    event_type VARCHAR(64) NOT NULL,
    card_id VARCHAR(64),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- What the issuer reported back, on the card row itself. Outcomes joins carry the analytical
-- copy (card_rail_outcomes, reported_by='reap'); these columns are the OPERATIONAL state the
-- webhook path reads and guards on.
ALTER TABLE agent_issued_cards ADD COLUMN IF NOT EXISTS last_auth_at TIMESTAMPTZ;
ALTER TABLE agent_issued_cards ADD COLUMN IF NOT EXISTS auth_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_issued_cards ADD COLUMN IF NOT EXISTS settled_amount_minor BIGINT;

-- The webhook path looks cards up by the ISSUER'S reference, not ours.
CREATE INDEX IF NOT EXISTS idx_agent_issued_cards_issuer_ref
    ON agent_issued_cards (issuer_card_ref);

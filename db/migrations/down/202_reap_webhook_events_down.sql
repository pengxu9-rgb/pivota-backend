DROP TABLE IF EXISTS reap_webhook_events;
ALTER TABLE agent_issued_cards DROP COLUMN IF EXISTS last_auth_at;
ALTER TABLE agent_issued_cards DROP COLUMN IF EXISTS auth_count;
ALTER TABLE agent_issued_cards DROP COLUMN IF EXISTS settled_amount_minor;
DROP INDEX IF EXISTS idx_agent_issued_cards_issuer_ref;

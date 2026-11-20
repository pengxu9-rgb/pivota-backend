-- Reverses 293: the step CHECK back to migration 289's ('card_binding', 'approval').
-- FAILS while any 'shipping_address' consent exists (the ADD checks the rows): those rows are evidence of what
-- a buyer accepted, so export them first, and only then delete them by hand if the step really goes away.
-- Switch AGENPAS_CONSENT_GATE off first (with it on and no shipping step, no address form is ever shown).
-- The boot heal (db/schema_guard -> db/agent_purchase_consents) widens the CHECK again at the next start
-- unless the code is reverted too.
SET LOCAL lock_timeout = '5s';

ALTER TABLE agent_purchase_consents DROP CONSTRAINT IF EXISTS ck_agent_purchase_consents_step;
ALTER TABLE agent_purchase_consents ADD CONSTRAINT ck_agent_purchase_consents_step
    CHECK (step IN ('card_binding', 'approval'));

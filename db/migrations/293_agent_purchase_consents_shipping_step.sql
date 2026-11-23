-- 293: THE A2A SHIPPING-ADDRESS STEP IS CONSENT-GATED TOO: agent_purchase_consents.step admits 'shipping_address'.
--
-- The owner's decision (11-20): the buyer accepts Pivota's terms and privacy policy on the AgenPas page before
-- the A2A rail asks them for a shipping address, as before a card page or an approval. One row per
-- (rail purchase, 'shipping_address', approval epoch 0), written by POST .../consents (dark).
--
-- WIDEN ONLY: the step CHECK gains 'shipping_address'; every existing row ('card_binding', 'approval') still
-- satisfies it. Idempotent (DROP CONSTRAINT IF EXISTS + ADD), under a short lock_timeout: the ADD takes ACCESS
-- EXCLUSIVE and checks the rows (a small table; prod holds none today). "shipping_address" is exactly 16
-- characters, the column's width (VARCHAR(16)).
--
-- The same widening is in db/agent_purchase_consents (_CREATE_TABLE_PG for a new table, and
-- _WIDEN_STEP_CHECK_DDL for one an earlier boot built with the 289 CHECK), run by
-- db/schema_guard.ensure_required_schema_light on every boot (a fast-mode boot skips db/migrations/):
-- tests/test_agenpas_consent_gate_postgres.py proves the two paths build the same schema, from an empty
-- database and from the 289 table with rows in it.

SET LOCAL lock_timeout = '5s';

ALTER TABLE agent_purchase_consents DROP CONSTRAINT IF EXISTS ck_agent_purchase_consents_step;
ALTER TABLE agent_purchase_consents ADD CONSTRAINT ck_agent_purchase_consents_step
    CHECK (step IN ('card_binding', 'approval', 'shipping_address'));

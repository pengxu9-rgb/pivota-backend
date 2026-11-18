-- Reverses 289. DESTROYS THE CONSENT RECORDS: export the table first if any row was ever written (they are
-- the evidence a payment partner asked for). Switch AGENPAS_CONSENT_GATE and AGENPAS_CONSENT_WRITE_ENABLED
-- off first: with the gate on and the table gone every card / approval page is withheld (fail closed).
DROP INDEX IF EXISTS uq_agent_purchase_consents_step;
DROP TABLE IF EXISTS agent_purchase_consents;

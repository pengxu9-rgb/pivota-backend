-- Reverse of 224_reap_agentic_ledger.sql.
--
-- Indexes first, then the tables. DROP TABLE would take its own indexes with it, but naming
-- them keeps this file readable as the inverse of the up-migration and survives a partial
-- apply where a table was created by the schema-guard self-heal and an index was not.
--
-- This DROPS the purchase ledger. Anything in flight (awaiting_approval / processing) loses its
-- poll bookkeeping and cannot be reconciled from our side afterwards — Reap's checkout is
-- unaffected and will still complete or expire on their side. Run it on a rail that is dark.
DROP INDEX IF EXISTS uq_reap_agentic_purchases_checkout;
DROP INDEX IF EXISTS idx_reap_agentic_purchases_owner_created;
DROP INDEX IF EXISTS idx_reap_agentic_purchases_buyer_created;
DROP INDEX IF EXISTS idx_reap_agentic_purchases_state_poll;
DROP TABLE IF EXISTS reap_agentic_purchases;

DROP INDEX IF EXISTS uq_reap_agentic_enrollments_one_active;
DROP INDEX IF EXISTS uq_reap_agentic_enrollments_reap_id;
DROP INDEX IF EXISTS idx_reap_agentic_enrollments_buyer_status;
DROP TABLE IF EXISTS reap_agentic_enrollments;

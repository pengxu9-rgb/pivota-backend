-- Reverse of 247_reap_agentic_purchase_offer_code.sql.
--
-- WHAT THIS DESTROYS: the buyer's offer code on every purchase, what Reap made of it, and the
-- discount Reap applied. Nothing else holds the code -- Reap's quote is gone within minutes.
--
-- IT DOES NOT FAIL CLOSED. db/reap_agentic_ledger.py names these columns in every purchase
-- INSERT and every transition, so dropping them makes EVERY purchase write raise UndefinedColumn
-- -- the rail stops rather than quietly losing codes. Roll the CODE back first; with the columns
-- present and nothing writing them, they cost nothing.
--
-- `IF EXISTS` on each column and on the table, one statement each, so a partial apply reverses
-- cleanly.
ALTER TABLE IF EXISTS reap_agentic_purchases DROP COLUMN IF EXISTS tax_included;
ALTER TABLE IF EXISTS reap_agentic_purchases DROP COLUMN IF EXISTS discount_minor;
ALTER TABLE IF EXISTS reap_agentic_purchases DROP COLUMN IF EXISTS offer_code_outcome;
ALTER TABLE IF EXISTS reap_agentic_purchases DROP COLUMN IF EXISTS offer_code;

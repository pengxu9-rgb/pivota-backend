-- 298: THE A2A RAIL'S PAYING CARD, AS THE BUYER PAGE MAY NAME IT (payment orchestration, AgenPas).
--
-- The card's network and last four, from the rail's own card list (`payment.method-list` methods[].payment_brand
-- and .last4), written as the token is minted on that card (services/payment_orchestration/a2a_rail_worker
-- `_record_payment_card`, through db/a2a_rail_ledger.record_payment_card_as_holder), with the id of the card they
-- belong to. The AgenPas internal read names the card only while that id is still the row's payment_method_id and the
-- purchase is awaiting the buyer's approval of the token, paying, or paid. Never the checkout answer's `cardLast4`:
-- that card is filled with the network token, so its last four are the token's, not the buyer's card.
--
-- Two display attributes the codebase already shows for the Reap rail (reap_agentic_enrollments); no other card
-- data. Nullable, no default: on Postgres 11+ a catalog-only change, no table rewrite.
--
-- schema-guard-exempt: the self-heal lives in db/a2a_rail_ledger.ensure_a2a_rail_schema (the same ALTER, run
-- through schema_guard.guarded_add_columns on Postgres and a pragma check on SQLite), which
-- db/schema_guard.ensure_required_schema_light calls at every boot on both dialects (a fast-mode boot skips
-- db/migrations/). tests/test_a2a_rail_ledger_postgres.py proves the two build the same schema.

ALTER TABLE IF EXISTS a2a_rail_purchases
  ADD COLUMN IF NOT EXISTS card_network VARCHAR(32),
  ADD COLUMN IF NOT EXISTS card_last4 VARCHAR(4),
  ADD COLUMN IF NOT EXISTS card_method_id VARCHAR(128);

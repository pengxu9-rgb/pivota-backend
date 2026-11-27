-- 301: THE BUYER ASKED TO ADD A NEW CARD FROM THE CARD CHOICE (payment orchestration, AgenPas).
--
-- With the card choice on (migration 300), the buyer may pick "Add a new card" instead of a saved one. The worker then
-- leaves the wait for the rail's card page (`quoting` -> `needs_payment_method`, only before any token), binds the new
-- card, takes it as the buyer's confirmed card (they chose "Add card and approve"), and locks the order again.
--
--   card_new_requested_at  when the buyer asked for a new card (db/a2a_rail_ledger.request_new_card). While set and
--                          unconfirmed, the binding waits for THAT card only, never another ACTIVE one.
--
-- Nullable, no default: on Postgres 11+ a catalog-only change, no table rewrite.
--
-- schema-guard-exempt: the self-heal lives in db/a2a_rail_ledger.ensure_a2a_rail_schema (the same ALTER, run
-- through schema_guard.guarded_add_columns on Postgres and a pragma check on SQLite), which
-- db/schema_guard.ensure_required_schema_light calls at every boot on both dialects (a fast-mode boot skips
-- db/migrations/). tests/test_a2a_rail_ledger_postgres.py proves the two build the same schema.

ALTER TABLE IF EXISTS a2a_rail_purchases
  ADD COLUMN IF NOT EXISTS card_new_requested_at TIMESTAMPTZ;

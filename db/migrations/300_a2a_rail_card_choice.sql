-- 300: THE BUYER'S CARD CHOICE ON THE A2A RAIL, BEFORE THE TOKEN IS MINTED (payment orchestration, AgenPas).
--
-- With AGENPAS_CARD_PICKER_ENABLED on, the worker does not mint a token on the buyer's newest card on its own: the
-- order is locked, then the purchase waits in `quoting` until the buyer confirms a card on the AgenPas page
-- (services/payment_orchestration/a2a_rail_worker `_await_card_choice`). The rail has no void, so the card cannot
-- change once the token is asked for: the choice is made before, or not at all.
--
--   card_options       the buyer's ACTIVE cards as the rail's own card list (`payment.method-list`) last named them,
--                      newest first, written by the worker as the purchase starts waiting: per card its rail id,
--                      network, last four and when it was added. One canonical JSON text, compared whole by the
--                      choice's compare-and-swap. Display attributes and opaque ids only (the same three the
--                      codebase already keeps for the paying card, migration 298), never card data.
--   card_confirmed_at  when the buyer confirmed the card the row names (db/a2a_rail_ledger.confirm_card_choice).
--                      The worker mints only once it is set (while the dial is on).
--
-- Nullable, no default: on Postgres 11+ a catalog-only change, no table rewrite.
--
-- schema-guard-exempt: the self-heal lives in db/a2a_rail_ledger.ensure_a2a_rail_schema (the same ALTER, run
-- through schema_guard.guarded_add_columns on Postgres and a pragma check on SQLite), which
-- db/schema_guard.ensure_required_schema_light calls at every boot on both dialects (a fast-mode boot skips
-- db/migrations/). tests/test_a2a_rail_ledger_postgres.py proves the two build the same schema.

ALTER TABLE IF EXISTS a2a_rail_purchases
  ADD COLUMN IF NOT EXISTS card_options TEXT,
  ADD COLUMN IF NOT EXISTS card_confirmed_at TIMESTAMPTZ;

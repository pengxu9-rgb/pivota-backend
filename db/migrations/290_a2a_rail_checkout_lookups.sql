-- 290: HOW MANY TIMES A HELD A2A CHECKOUT WAS LOOKED UP (payment orchestration, the checkout relay).
--
-- A checkout held because an answer was never seen is looked up, read-only, by Pivota's idempotency key
-- (a2a_rail_worker._checkout_lookup). Each lookup is counted HERE, write-ahead, before it is sent
-- (db/a2a_rail_ledger.count_checkout_lookup_as_holder), so the cap holds even when a step crashes in the
-- middle of a lookup. Past the cap the purchase stays held for a human. A count, never PII.
--
-- Nullable, no default: on Postgres 11+ a catalog-only change, no table rewrite. Only the dark checkout
-- step (A2A_RAIL_CHECKOUT_STEP_ENABLED, default off) reads or writes it; a row read without it is never
-- looked up.
--
-- schema-guard-exempt: the self-heal lives in db/a2a_rail_ledger.ensure_a2a_rail_schema (the same
-- ALTER, run through schema_guard.guarded_add_columns on Postgres and a pragma check on SQLite), which
-- db/schema_guard.ensure_required_schema_light calls at every boot on both dialects (a fast-mode boot
-- skips db/migrations/). tests/test_a2a_rail_ledger_postgres.py proves the two build the same schema.

ALTER TABLE IF EXISTS a2a_rail_purchases
  ADD COLUMN IF NOT EXISTS checkout_lookups INTEGER;

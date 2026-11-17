-- 288: HOW THE A2A RAIL CHECKOUT PATH'S APPROVAL CEILING WAS BUILT (payment orchestration).
--
-- The A2A rail provider's checkout skill gives no quote before ordering, so on that path Pivota locks the
-- order itself (services/payment_orchestration/a2a_rail_worker.checkout_ceiling): its own order reference
-- in `order_id`, and in `quoted_total_minor` an approval CEILING (our price x quantity plus a tax and a
-- shipping allowance), which the network token is then minted for. This column says how that ceiling was
-- built (`allowance_v1` today; a merchant's own quote later), so the agent view can say "you approve up to
-- this; the final total includes shipping and tax". Written only with the ceiling
-- (db/a2a_rail_ledger.lock_checkout_order_as_holder). A short code, never PII.
--
-- Nullable, no default: on Postgres 11+ a catalog-only change, no table rewrite. Only the dark checkout
-- step (A2A_RAIL_CHECKOUT_STEP_ENABLED, default off) writes it.
--
-- schema-guard-exempt: the self-heal lives in db/a2a_rail_ledger.ensure_a2a_rail_schema (the same
-- ALTER, run through schema_guard.guarded_add_columns on Postgres and a pragma check on SQLite), which
-- db/schema_guard.ensure_required_schema_light calls at every boot on both dialects (a fast-mode boot
-- skips db/migrations/). tests/test_a2a_rail_ledger_postgres.py proves the two build the same schema.

ALTER TABLE IF EXISTS a2a_rail_purchases
  ADD COLUMN IF NOT EXISTS ceiling_source VARCHAR(32);

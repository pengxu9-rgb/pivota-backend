-- 287: THE A2A TOKEN RAIL'S CHECKOUT TASK ID (payment orchestration, the checkout relay).
--
-- Once the buyer has approved the network token, the worker relays it into the A2A rail provider's
-- checkout skill, which fills and submits the merchant's checkout itself. The provider answers the
-- confirm with a task id (`ckout_...`); the worker records it here, in the statement that resolves the
-- confirm's write-ahead (db/a2a_rail_ledger.record_checkout_task_as_holder), and polls / looks the
-- submission up by it until the merchant's answer. The id is the provider's, not a credential; the
-- agent-facing view never shows it.
--
-- Nullable, no default: on Postgres 11+ a catalog-only change, no table rewrite. Only the checkout
-- step reads or writes it, and that step is dark (A2A_RAIL_CHECKOUT_STEP_ENABLED, default off), so
-- every other statement works on a table that does not have it yet.
--
-- schema-guard-exempt: the self-heal lives in db/a2a_rail_ledger.ensure_a2a_rail_schema (the same
-- ALTER, run through schema_guard.guarded_add_columns on Postgres and a pragma check on SQLite), which
-- db/schema_guard.ensure_required_schema_light calls at every boot on both dialects (a fast-mode boot
-- skips db/migrations/). tests/test_a2a_rail_ledger_postgres.py proves the two build the same schema.

ALTER TABLE IF EXISTS a2a_rail_purchases
  ADD COLUMN IF NOT EXISTS checkout_task_id VARCHAR(128);

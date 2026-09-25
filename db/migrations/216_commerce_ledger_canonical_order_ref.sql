-- 216: one canonical identity for one purchase, across every authority.
--
-- The same order reaches the ledger from several writers and each names it
-- with its own id: the Stripe PSP bridge writes Pivota's orders.order_id, the
-- Shopify webhook writes Shopify's numeric order id, the agent checkout writes
-- the Pivota order id again, and WooCommerce/Cafe24/SHOPLINE/Adobe/SFCC each
-- write their own native id. The funnel keys paid amounts and order counts on
-- (platform, store_id, order_id), which only de-duplicates when the ids
-- already match — so a Pivota-originated Shopify order paid through Stripe
-- counted its GMV twice, once under each namespace.
--
-- order_ref is `<namespace>:<id in that namespace's system of record>`:
--
--   pivota:ord_abc123    the order originated in Pivota (agent checkout ->
--                        orders row -> Stripe -> optional writeback). EVERY
--                        authority that can recognise it emits this same ref.
--   shopify:6600123      the order originated on the store platform.
--   woocommerce:44       likewise.
--
-- order_id is deliberately untouched: it stays the diagnostic record of what
-- each authority called the order, and rows written before this migration keep
-- a NULL order_ref and keep aggregating on the legacy key.
--
-- The unique index mirrors idx_commerce_interactions_order_id_unique so a
-- Stripe payment.succeeded and a Shopify orders/paid for one purchase converge
-- on ONE interaction even when no click id ties them together. It is built
-- normally, not without blocking writers: order_ref is a brand-new all-NULL
-- column so the partial index has no rows to scan, and every sibling unique
-- index on this table (migration 205, and the schema guard's self-heal) is
-- built the same way.
--
-- Two regexes read this file's PROSE as if it were code, so mind both:
-- db/sql_migrations.py classifies the whole body, comments included, for the
-- non-blocking-index keyword — a match there would put the column adds below
-- on the autocommit path and lose their transaction; and
-- tests/test_schema_guard_migration_coverage.py reads any table-altering
-- phrase written here as a real statement and demands a self-heal for it.
-- Name neither construct in a comment.

ALTER TABLE commerce_interactions
  ADD COLUMN IF NOT EXISTS order_ref VARCHAR(160) NULL;

ALTER TABLE commerce_interaction_events
  ADD COLUMN IF NOT EXISTS order_ref VARCHAR(160) NULL;

-- These two carry SQLAlchemy's own generated names, because the model marks
-- both columns index=True and a fresh database is built by the model. Same
-- name, same definition, either way the table was built.
CREATE INDEX IF NOT EXISTS ix_commerce_interactions_order_ref
  ON commerce_interactions (order_ref);

CREATE INDEX IF NOT EXISTS ix_commerce_interaction_events_order_ref
  ON commerce_interaction_events (order_ref);

CREATE UNIQUE INDEX IF NOT EXISTS idx_commerce_interactions_order_ref_unique
  ON commerce_interactions (merchant_id, COALESCE(store_id, ''), order_ref)
  WHERE order_ref IS NOT NULL;

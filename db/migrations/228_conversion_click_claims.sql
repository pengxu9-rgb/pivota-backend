-- ONE EDGE PER CART-LINK CLICK: a first-writer-wins claim, taken WITHOUT a transaction.
--
-- THE DEFECT THIS CLOSES. A Reap cart-link purchase (mig 226) is ONE sale that TWO channels can
-- close. Reap reports it (services/reap_agentic_purchase._close_attribution closes under
-- (merchant_domain, Reap orderId)). The merchant's own Shopify order ALSO carries our
-- `pivota_click_id` cart attribute, because the permalink put it there, so the `orders/paid`
-- webhook (routes/webhook_routes.py) and the read_orders poller
-- (services/external_conversion_poller.py) close it under (tenant merchant_id, Shopify order id).
-- The edge table's only dedupe is ON CONFLICT (merchant_id, external_order_id), and those two
-- keys never collide: two edges, double GMV, for one sale. The variant lane never had this,
-- because its merchant order carries no click id.
--
-- THE CLAIM. Whichever channel closes first INSERTs the click id here with
-- `ON CONFLICT (click_id) DO NOTHING RETURNING click_id`. A returned row means it owns the edge;
-- no row means the other channel already did, and it skips. The PRIMARY KEY is the whole
-- concurrency guard: one statement, atomic on both engines, NO transaction. That is not a style
-- choice. On this app's shared `databases` connection, transaction statements bypass the query
-- lock and silently lose writes, so neither `database.transaction()` nor an advisory xact lock
-- is available here.
--
-- SCOPE. Only clicks that belong to a cart_link Reap purchase are ever claimed. The merchant side
-- looks that up by click id first, and every other click never touches this table. That lookup
-- is on reap_agentic_purchases.click_id, which had no index, hence the second statement.
--
--   claimed_by        — 'reap_agentic' or 'merchant_order' (the webhook and the poller close
--                       the same Shopify order under the same key, so they are ONE claimant)
--   external_order_id — the order id the claimant closes under. A same-claimant retry for the
--                       same order proceeds (the edge close is idempotent); anything else skips.
--
-- Production deploys skip db/migrations/, so both statements are ALSO in
-- db/schema_guard.ensure_required_schema_light, each in its own try. The two builds are compared
-- through the catalog by tests/test_conversion_click_claims_postgres.py.

CREATE TABLE IF NOT EXISTS conversion_click_claims (
    click_id TEXT PRIMARY KEY,
    claimed_by TEXT NOT NULL
        CONSTRAINT ck_conversion_click_claims_claimed_by
        CHECK (claimed_by IN ('reap_agentic', 'merchant_order')),
    external_order_id TEXT,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reap_agentic_purchases_click_id
    ON reap_agentic_purchases (click_id);

-- ITEM SOURCE ON THE PURCHASE ROW: what the quote is asked to price.
--
--   reap_variant — the row names OUR catalog object and the resolver turns it into a Reap
--                  `var_` id before every quote. Every row before this migration is one of
--                  these, and the DEFAULT makes that true of every row it touches.
--   cart_link    — the row carries a Shopify CART PERMALINK
--                  (`https://<shop>/cart/<variant>:<qty>?attributes[pivota_click_id]=<clk>`)
--                  which Reap quotes and checks out AS RECEIVED. This is the Tier B lane: the
--                  merchant's own agent checkout refuses us, and the cart attribute is what
--                  carries our click id onto the merchant's order.
--
-- THE PAIRING IS A DATABASE FACT, NOT ONLY A PYTHON ONE. A cart_link row without a URL has
-- nothing to quote, and a reap_variant row WITH one is a row two code paths would each read
-- differently. db/reap_agentic_ledger.create_purchase refuses both before the INSERT; the CHECK
-- below is what refuses them for any other writer.
--
-- cart_url IS NOT PII AND IS NOT NULLED ON TERMINAL. services/reap_cart_link.validate_cart_link
-- admits exactly one query key (our click attribute) and a path of digits, so a `checkout[...]`
-- key carrying the buyer's email or address can never be stored here. It stays on a terminal
-- row for the same reason the resolution hints do: it is the record of what was bought.
-- It is NOT in db/reap_agentic_ledger.PUBLIC_PURCHASE_COLUMNS, and neither is item_source.
--
-- NEITHER COLUMN IS A TRANSITION FIELD. Both are written by create_purchase and never again:
-- a poller step that could rewrite the URL could change what the buyer is charged for.
--
-- ONE STATEMENT, COLUMN CONSTRAINTS, IF NOT EXISTS. The CHECKs ride on the column definitions,
-- so the `IF NOT EXISTS` that makes the statement idempotent also skips its constraints on a
-- second run — no duplicate constraint, no DO block. item_source is added before cart_url in
-- the same statement, which is what lets the pairing CHECK name it. Measured on Postgres 15:
-- a second run adds nothing and the catalog is unchanged.
--
-- Production deploys skip db/migrations/, so this statement is ALSO in
-- db/schema_guard.ensure_required_schema_light, in its own try block after the mig-225 one.
-- The two must build the SAME SCHEMA; tests/test_reap_agentic_cart_link_postgres.py compares
-- the catalog built by each (columns and every CHECK's pg_get_constraintdef), and
-- tests/test_reap_agentic_ledger_postgres.py's whole-table parity test covers it too.

ALTER TABLE IF EXISTS reap_agentic_purchases
    ADD COLUMN IF NOT EXISTS item_source TEXT NOT NULL DEFAULT 'reap_variant'
        CONSTRAINT ck_reap_agentic_purchases_item_source
        CHECK (item_source IN ('reap_variant', 'cart_link')),
    ADD COLUMN IF NOT EXISTS cart_url TEXT
        CONSTRAINT ck_reap_agentic_purchases_cart_url_pairing
        CHECK (
            (item_source = 'reap_variant' AND cart_url IS NULL)
            OR (item_source = 'cart_link' AND cart_url IS NOT NULL)
        );

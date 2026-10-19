-- 269: agent_pdp_view.seller_count -- how many SELLERS price the product (2026-10-19).
--
-- offer_count counts offers, and every reader read it as sellers: routes/agent_pdp_v1 scoped a row
-- `multi_merchant_canonical` at offer_count > 1, pivota-agent-ui marks a card multi-seller (and
-- drops its savings badge) at offers_count > 1, PIVOTA-Agent's discovery cards pass it through. A
-- product's offers are per variant, and a retailer row also carries its `<product_key>::canonical`
-- placeholder beside them, so one store read as several. Read-only prod census, 2026-10-19: of
-- 26,763 rows, 18,927 had offer_count > 1 and 1,845 have more than one seller. offer_count now drops
-- the placeholder twins (11,164 rows fall to 1) and 5,918 stay above 1 on one store's shades or
-- sizes; this column is the count the readers meant.
--
-- seller_count (written by services/agent_pdp_view_assembler.count_sellers): the distinct sellers
-- -- merchant on the listing's host -- of the row's priced offers, placeholder twins dropped, every
-- currency. Each market_prices entry carries its currency's own seller_count.
--
-- NULLABLE, no default, no index: read only off a row already found by its key. NULL = written by
-- code older than this migration (or never rebuilt); readers then fall back to offer_count. Not on
-- db/catalog.py's agent_pdp_view Table (agent_pdp_view.select() callers would name it before a heal
-- lands); readers select it through agent_pdp_view_assembler.seller_count_sql. Production deploys
-- skip db/migrations/, so db/schema_guard.py adds the same column at boot. Fill: a rebuild
-- (scripts/backfill_agent_pdp_view.py).

ALTER TABLE IF EXISTS agent_pdp_view
    ADD COLUMN IF NOT EXISTS seller_count INTEGER;

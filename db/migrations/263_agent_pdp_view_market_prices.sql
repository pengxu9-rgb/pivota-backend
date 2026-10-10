-- 263: agent_pdp_view.market_prices -- the price summary PER SERVED MARKET (2026-10-10).
--
-- The row-level currency / price_min / price_max are ONE currency, the modal one (ties to the
-- higher code), so a product with USD offers and their SGD siblings (retailer_ingest
-- shopify_markets, market 'SG') reads as a USD product and every surface serving an SG buyer
-- from those columns drops it. The top-5 `offers` cut also sorts raw prices across currencies,
-- so the SGD offers can be cut from the row entirely.
--
-- Shape (written by services/agent_pdp_view_assembler.build_market_prices):
--   {"US": {"currency": "USD", "price_min": n, "price_max": n, "offer_count": n, "offers": [...]},
--    "SG": {"currency": "SGD", ...}}
-- keyed by served pricing region (PIVOTA_SERVING_PRICING_REGIONS); an entry covers the offers
-- priced in that region's currency, never converted. NULL = not computed yet (readers fall back
-- to the legacy columns, which are unchanged); {} = computed, nothing priced for a served region.
--
-- NULLABLE, no default, no index: it is read only off a row already found by its key, and written
-- only while AGENT_PDP_VIEW_MARKET_PRICES is on. Production deploys skip db/migrations/, so
-- db/schema_guard.py adds the same column at boot. Fill: scripts/backfill_agent_pdp_view.py
-- --scope market_prices_missing.

ALTER TABLE IF EXISTS agent_pdp_view
    ADD COLUMN IF NOT EXISTS market_prices JSONB;

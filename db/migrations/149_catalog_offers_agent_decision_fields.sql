-- Migration 149: catalog_offers agent-decision fields (K-beauty common-core hardening)
-- The agent-decision-grade data contract's Offers/buy-box layer requires every
-- offer to carry: offer_type (brand_direct | retailer), market, is_first_party,
-- and a why_buy_direct rationale -- so an agent can resolve a best_us_offer and
-- frame "why buy direct" honestly.
--
-- catalog_offers rows are all internal_merchant (the sync write path hardcodes
-- that track; external retailer offers are built in-memory from external_product
-- _seeds at query time). So the deterministic backfill below is:
--   * is_first_party = TRUE, market = 'US' for every internal row
--   * offer_type = 'brand_direct' ONLY when the merchant is explicitly verified
--     as the brand (catalog_merchants.metadata_json.brand_relationship) -- we do
--     NOT assume internal == brand_direct, per the contract's VERIFIED rule.
-- offer_type stays NULL for unverified internal merchants (honest "unknown"),
-- and why_buy_direct stays NULL until authored.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + re-runnable backfills.
BEGIN;

ALTER TABLE catalog_offers ADD COLUMN IF NOT EXISTS offer_type VARCHAR(16);
ALTER TABLE catalog_offers ADD COLUMN IF NOT EXISTS market VARCHAR(8) NOT NULL DEFAULT 'US';
ALTER TABLE catalog_offers ADD COLUMN IF NOT EXISTS is_first_party BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE catalog_offers ADD COLUMN IF NOT EXISTS why_buy_direct TEXT;

-- Internal merchant offers are first-party, US-market by default (the cohort is
-- US-market-primary; refine to real per-offer geo when modeled).
UPDATE catalog_offers
   SET is_first_party = TRUE,
       market = 'US'
 WHERE catalog_track = 'internal_merchant';

-- Defensive: any external rows that exist are retailer / not first-party.
UPDATE catalog_offers
   SET offer_type = 'retailer',
       is_first_party = FALSE
 WHERE catalog_track = 'external_referral';

-- brand_direct only for verified brand merchants.
UPDATE catalog_offers o
   SET offer_type = 'brand_direct'
  FROM catalog_merchants m
 WHERE o.merchant_id = m.merchant_id
   AND o.catalog_track = 'internal_merchant'
   AND lower(m.metadata_json->>'brand_relationship') = 'brand_direct'
   AND o.offer_type IS DISTINCT FROM 'brand_direct';

-- PR4's serving gate queries ">=1 US-buyable offer" by (product_key, market) and
-- filters by offer_type; index both access paths.
CREATE INDEX IF NOT EXISTS idx_catalog_offers_product_market
    ON catalog_offers (product_key, market);
CREATE INDEX IF NOT EXISTS idx_catalog_offers_market_offer_type
    ON catalog_offers (market, offer_type);

COMMIT;

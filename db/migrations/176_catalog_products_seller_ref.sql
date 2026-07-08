-- 176_catalog_products_seller_ref.sql
-- Convergence plan Phase 1.2 (docs: PIVOTA-Agent plan "one spine"): promote the
-- ADR-009 seller-of-record identity from external_product_seeds onto the
-- CANONICAL product row, so records that never touch the seed table can still
-- thread attribution.
--
-- WHY: the crawl→audit intake door (services/audit_index_intake.py, the new
-- mainline) writes catalog_products DIRECTLY with no external_product_seeds
-- row. The attributed-redirect lane resolves seller_ref by joining the seed
-- table (agent_shop_gateway._external_seed_redirect_identity via
-- attached_product_key), so audit-door records have NO seller_ref source and
-- conversion closure stamps `seller_ref_missing`. Same column semantics as
-- migration 169:
--
--   seller_ref → a catalog_merchants.merchant_id (the seller-of-record; for
--                audit seeds: the auditing merchant when it owns the
--                destination domain, else the observed seller minted by
--                services/seller_identity.ensure_observed_seller).
--   seed_kind  → 'self' | 'cross'; NULL = legacy/underivable (never silently
--                treated as 'self' — ADR-009 D3 no-fallback).
--
-- catalog_offers intentionally NOT touched here: offers inherit seller
-- identity when the external-offer dual-write lands (Phase 1.6); the audit
-- door writes product rows only.
--
-- Additive + nullable; existing writers that omit the columns leave them NULL
-- → behavior byte-identical until the audit-door stamping lands.
--
-- Idempotent DDL. Railway prod SKIPS db/migrations/ — the matching self-heal
-- in db/schema_guard.ensure_required_schema_light brings these columns into
-- prod at startup (migration-167/168/169 idiom); the coverage gate test
-- enforces the mirror.
BEGIN;

ALTER TABLE IF EXISTS catalog_products
  ADD COLUMN IF NOT EXISTS seller_ref TEXT,   -- catalog_merchants.merchant_id (seller-of-record)
  ADD COLUMN IF NOT EXISTS seed_kind TEXT;    -- 'self' | 'cross' (NULL = legacy/underivable)

-- Honesty guard: only the two derived kinds; NULL allowed (migration-169 idiom).
ALTER TABLE IF EXISTS catalog_products
  DROP CONSTRAINT IF EXISTS ck_catalog_products_seed_kind;
ALTER TABLE IF EXISTS catalog_products
  ADD CONSTRAINT ck_catalog_products_seed_kind
  CHECK (seed_kind IS NULL OR seed_kind IN ('self', 'cross'));

COMMIT;

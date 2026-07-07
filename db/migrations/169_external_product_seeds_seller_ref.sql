-- 169_external_product_seeds_seller_ref.sql
-- ADR-009 D3 (docs/adr/ADR-009-seller-of-record-identity.md), packet A9-3:
-- external seeds gain an explicit SELLER-OF-RECORD so the attribution chain can
-- key conversions by SELLER (with the anchor merchant as a separate surface
-- dimension). See docs/IDENTITY_REFERENCE.md §3 (Trap T3 — tenant/seller
-- conflation) and §4 (offer/attribution identifiers).
--
--   seller_ref  → a catalog_merchants.merchant_id (the seller-of-record). For a
--                 SELF seed this is the anchor merchant; for a CROSS seed it is
--                 the observed/claimed seller minted by A9-2
--                 (services/seller_identity.ensure_observed_seller).
--   seed_kind   → 'self' (destination == anchor's own store) | 'cross'
--                 (destination is a different seller than the anchor).
--
-- NULL on both columns = PRE-BACKFILL legacy state. A9-4 backfills the ~9.4k
-- existing rows via the W1 RunFacts parity pattern. Per the founder no-fallback
-- directive, NULL is NEVER silently treated as 'self' anywhere downstream — the
-- threading/closure code observes NULL (metadata.seller_ref_missing) instead of
-- assuming a value, so A9-4 has a kill metric.
--
-- Additive columns only; every existing reader/writer that omits these leaves
-- them NULL → behavior byte-identical until A9-3's write-time derivation lands.
--
-- Idempotent DDL. Railway prod SKIPS db/migrations/ (the apm_enabled outage
-- class) — the matching self-heal in db/schema_guard.ensure_required_schema_light
-- brings these columns into prod at startup (migration-167/168 idiom). The
-- schema-guard coverage gate (tests/test_schema_guard_migration_coverage.py)
-- enforces that mirror for every new column below.
BEGIN;

ALTER TABLE IF EXISTS external_product_seeds
  ADD COLUMN IF NOT EXISTS seller_ref TEXT,   -- catalog_merchants.merchant_id (seller-of-record)
  ADD COLUMN IF NOT EXISTS seed_kind TEXT;    -- 'self' | 'cross' (NULL = pre-A9-4 legacy)

-- Honesty guard: only the two derived kinds; NULL allowed for legacy/pre-backfill
-- rows. Mirrors the migration-167 ck_..._state pattern (drop-then-add for
-- idempotent re-application).
ALTER TABLE IF EXISTS external_product_seeds
  DROP CONSTRAINT IF EXISTS ck_external_product_seeds_seed_kind;
ALTER TABLE IF EXISTS external_product_seeds
  ADD CONSTRAINT ck_external_product_seeds_seed_kind
  CHECK (seed_kind IS NULL OR seed_kind IN ('self', 'cross'));

-- Seller-keyed reads (A9-4 backfill parity scans + per-seller offer lookups).
CREATE INDEX IF NOT EXISTS idx_external_product_seeds_seller_ref
  ON external_product_seeds (seller_ref)
  WHERE seller_ref IS NOT NULL;

COMMENT ON COLUMN external_product_seeds.seller_ref IS 'Seller-of-record catalog_merchants.merchant_id (ADR-009 D3). NULL = pre-A9-4 legacy.';
COMMENT ON COLUMN external_product_seeds.seed_kind IS 'self | cross (ADR-009 D3): destination == anchor store vs a different seller. NULL = pre-A9-4 legacy.';

COMMIT;

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- ALTER TABLE IF EXISTS external_product_seeds DROP CONSTRAINT IF EXISTS ck_external_product_seeds_seed_kind;
-- DROP INDEX IF EXISTS idx_external_product_seeds_seller_ref;
-- ALTER TABLE IF EXISTS external_product_seeds DROP COLUMN IF EXISTS seed_kind;
-- ALTER TABLE IF EXISTS external_product_seeds DROP COLUMN IF EXISTS seller_ref;

-- 084_catalog_products_sync_hygiene.sql
--
-- Stage 2a of the PDP architecture roadmap (plans/rosy-mixing-bengio.md).
-- Solves the "old expired caches" duplicate class — the MOYU cohort
-- (60 rows of 3 actual products, every other row a stale Shopify
-- product ID from before the merchant deleted+recreated).
--
-- Industry-standard pattern for catalog indexers (Algolia, Searchspring,
-- Shopify Search & Discovery): track when each row was last touched by
-- a successful upstream sync, then sweep rows that fall behind. Three
-- pieces here:
--
--   1. catalog_products.last_seen_in_sync_at — set by Path A's
--      _upsert_by_pk on every write. NULL means "row predates this
--      tracking" (legacy rows from before mig 084).
--
--   2. catalog_products.sync_status — 'live' | 'stale' | 'archived'.
--      Default 'live'. Sweep flips to 'stale' when last_seen falls
--      behind the merchant's last_full_sync_at by GRACE_HOURS. Stale
--      rows older than ARCHIVE_DAYS become 'archived'. Recall layer
--      filters on 'live'.
--
--   3. catalog_merchants.last_full_sync_at — Path A bumps this on
--      sync completion (last successful full pass). The sweep
--      compares per-row last_seen against per-merchant last_full_sync.
--      Without this, we'd need a sync-runs table; this is the
--      pragmatic minimum.
--
-- Out of scope: external_seed (merchant_id='external_seed') rows have
-- a different lifecycle (Path B/C rescrape on schedule, not synced
-- from a connected store). The sweep excludes them by filtering on
-- catalog_merchants.last_full_sync_at IS NOT NULL.
--
-- Idempotent: ADD COLUMN / CREATE INDEX with IF NOT EXISTS.

-- ---------------------------------------------------------------------
-- 1. catalog_products.last_seen_in_sync_at
-- ---------------------------------------------------------------------
ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS last_seen_in_sync_at TIMESTAMPTZ;

COMMENT ON COLUMN catalog_products.last_seen_in_sync_at IS
  'Path A only: timestamp of the most recent _upsert_by_pk that touched this row. NULL on rows predating mig 084 OR rows from non-sync paths (external_seed mirror, enrichment agent). Used by scripts/sweep_stale_catalog_products.py to detect Shopify deletes. See plans/rosy-mixing-bengio.md Stage 2a.';


-- ---------------------------------------------------------------------
-- 2. catalog_products.sync_status
-- ---------------------------------------------------------------------
--
-- Default 'live' so all existing rows stay in recall until the sweep
-- explicitly flips them. CHECK constraint enforces the enum at the
-- DB layer so callers can't accidentally write garbage.

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS sync_status VARCHAR(16) NOT NULL DEFAULT 'live';

-- Add the CHECK constraint idempotently. Postgres doesn't have
-- "ADD CONSTRAINT IF NOT EXISTS" pre-15, so use a DO block.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'catalog_products_sync_status_check'
  ) THEN
    ALTER TABLE catalog_products
      ADD CONSTRAINT catalog_products_sync_status_check
      CHECK (sync_status IN ('live', 'stale', 'archived'));
  END IF;
END $$;

COMMENT ON COLUMN catalog_products.sync_status IS
  'Path A sync lifecycle: live | stale | archived. Default live. Sweep flips to stale when last_seen_in_sync_at falls behind catalog_merchants.last_full_sync_at by GRACE_HOURS (24h default). Stale → archived after 7d. Recall layer (services/pivot_query_service.py) filters on sync_status=live. See plans/rosy-mixing-bengio.md Stage 2a.';


-- Partial index on the non-live rows. Most recall queries filter
-- sync_status='live' (the default), so we don't need a B-tree on the
-- whole column. The sweep itself + admin dashboards do scan for stale
-- / archived rows — the partial index makes those queries cheap.

CREATE INDEX IF NOT EXISTS idx_catalog_products_sync_status_non_live
  ON catalog_products (sync_status, last_seen_in_sync_at)
  WHERE sync_status != 'live';


-- ---------------------------------------------------------------------
-- 3. catalog_merchants.last_full_sync_at
-- ---------------------------------------------------------------------
--
-- Path A bumps this when a full sync completes successfully (the
-- transaction wrapping ingest_standard_products commits). The sweep
-- uses it as the "ground truth" timestamp to compare row last_seen
-- against — without it we couldn't tell "this merchant hasn't synced
-- recently" from "this row was deleted from the merchant's store."

ALTER TABLE catalog_merchants
  ADD COLUMN IF NOT EXISTS last_full_sync_at TIMESTAMPTZ;

COMMENT ON COLUMN catalog_merchants.last_full_sync_at IS
  'Timestamp of the merchant''s most recent successful Path A full sync (ingest_standard_products commit). NULL means no Path A sync has run for this merchant yet (e.g. external_seed which is Path B/C only). See plans/rosy-mixing-bengio.md Stage 2a.';

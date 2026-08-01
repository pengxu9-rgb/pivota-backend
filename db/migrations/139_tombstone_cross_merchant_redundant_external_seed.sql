-- 139_tombstone_cross_merchant_redundant_external_seed.sql
--
-- Layer C1 Phase 3a follow-up: tombstone cross-merchant external_seed
-- duplicates that the merchant now owns first-party.
--
-- Context
-- -------
-- When Pivota seeds a product as external_seed and the merchant later
-- onboards as a first-party catalog source (Shopify / Wix / etc.), the
-- external_seed row becomes redundant — the merchant IS the source of truth
-- for their own product, and the seed mirror is stale by construction.
--
-- The 2026-05-25 catalog merge audit (project_codex_catalog_merge_task)
-- flagged 47 such content_key groups (50 rows) — MOYU, GR, PawStyle,
-- The Ordinary, Winona — and noted the cleanup was blocked on missing
-- pdp_identity_listing rows. c1.v0.3 (PIVOTA-Agent #1560 /
-- pivota-backend #673) removed that dependency: first-party rows no longer
-- need identity to serve, so they no longer need identity to tombstone
-- their external_seed mirrors either.
--
-- The 50 rows were band-aided into catalog_source_quarantine to keep them
-- from serving, but they still lived in catalog_products and confused the
-- discoveryFeed brand-candidates LATERAL JOIN (PIVOTA-Agent #1567 fixes
-- the mapper to ignore tombstoned external_seed rows).
--
-- Predicate
-- ---------
-- Row qualifies as a redundant cross-merchant external_seed mirror when:
--   1. merchant_id = 'external_seed'
--   2. sync_status = 'live' (not already inactive)
--   3. suppression_reason IS NULL (not already tombstoned for another reason)
--   4. content_key IS NOT NULL
--   5. AT LEAST ONE first-party sibling exists with the same content_key
--      where sibling.merchant_id <> 'external_seed', sync_status='live',
--      suppression_reason IS NULL.
--
-- Idempotent: re-runs are no-ops because clause (3) excludes already-set
-- rows. Future cross-merchant duplicates will be caught when this migration
-- is re-applied (next time it runs as part of a fresh boot).
--
-- Producer safety
-- ---------------
-- PIVOTA-Agent/scripts/sync-external-seeds-to-catalog.cjs does NOT include
-- suppression_reason in its INSERT column list or DO UPDATE SET (verified
-- 2026-05-27). Re-syncing external_seeds will not clear this tombstone.
--
-- catalog_source_quarantine rules currently blocking these 50 rows are
-- intentionally left in place. They are now redundant (the trust policy
-- checks quarantine first, then tombstone — both gate the row to blocked),
-- but their removal is a separate cleanup PR.
--
-- Trust derivation
-- ----------------
-- After this migration, the next catalog_row_trust backfill (6h APScheduler
-- cron or ad-hoc) will keep these rows blocked. The serving_reason_codes
-- list will continue to show SOURCE_QUARANTINED because quarantine wins
-- over tombstone in catalogTrustPolicy.deriveSourceLifecycle. That's fine
-- — both reasons block the row from serving, and reason-code semantics are
-- explanatory not gating.

BEGIN;

UPDATE catalog_products
SET suppression_reason = 'cross_merchant_redundant_external_seed',
    -- P1a (#1648): suppressed_at is THE gate column; the label alone leaves
    -- the row serving. COALESCE keeps an existing timestamp intact.
    suppressed_at = COALESCE(suppressed_at, now()),
    updated_at = now()
WHERE merchant_id = 'external_seed'
  AND sync_status = 'live'
  AND suppression_reason IS NULL
  AND content_key IS NOT NULL
  AND EXISTS (
    SELECT 1
    FROM catalog_products sibling
    WHERE sibling.content_key = catalog_products.content_key
      AND sibling.merchant_id <> 'external_seed'
      AND sibling.sync_status = 'live'
      AND sibling.suppression_reason IS NULL
  );

COMMIT;

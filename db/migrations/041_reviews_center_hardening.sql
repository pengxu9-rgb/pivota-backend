-- Migration 041: Reviews Center hardening (import dedupe + media public_id)
-- PostgreSQL only.

-- ---------------------------------------------------------------------------
-- Risk fix #1: import dedupe must not collide cross-merchant
-- ---------------------------------------------------------------------------

DROP INDEX IF EXISTS ux_product_reviews_source_external;
DROP INDEX IF EXISTS ux_product_reviews_merchant_source_external;
DROP INDEX IF EXISTS ux_import_items_source_external_review;
DROP INDEX IF EXISTS ux_import_items_merchant_source_external_review;

CREATE UNIQUE INDEX IF NOT EXISTS ux_product_reviews_merchant_source_external
  ON product_reviews (merchant_id, source_system, external_review_id)
  WHERE external_review_id IS NOT NULL
    AND external_review_id <> ''
    AND source_system IS NOT NULL
    AND source_system <> '';

CREATE UNIQUE INDEX IF NOT EXISTS ux_import_items_merchant_source_external_review
  ON import_items (merchant_id, source_system, external_review_id)
  WHERE external_review_id IS NOT NULL
    AND external_review_id <> ''
    AND source_system IS NOT NULL
    AND source_system <> '';

-- ---------------------------------------------------------------------------
-- Risk fix #3: make review media URL unguessable via public_id
-- ---------------------------------------------------------------------------

ALTER TABLE media_assets
  ADD COLUMN IF NOT EXISTS public_id TEXT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_media_assets_public_id
  ON media_assets (public_id)
  WHERE public_id IS NOT NULL AND public_id <> '';

-- ---------------------------------------------------------------------------
-- Risk fix #2: imported reviews must have stable external identifiers
-- ---------------------------------------------------------------------------

ALTER TABLE product_reviews
  ADD COLUMN IF NOT EXISTS _tmp_noop BOOLEAN; -- no-op to keep older migration runners happy

-- Add the CHECK constraint as NOT VALID to avoid failing the migration on existing
-- legacy rows that may have NULL identifiers; the constraint still applies to
-- all new writes.
DO $$
BEGIN
  ALTER TABLE product_reviews
    ADD CONSTRAINT chk_product_reviews_imported_requires_external_ids
    CHECK (
      source_type <> 'imported'
      OR (
        COALESCE(source_system, '') <> ''
        AND COALESCE(external_review_id, '') <> ''
      )
    )
    NOT VALID;
EXCEPTION
  WHEN duplicate_object THEN
    NULL;
END $$;

ALTER TABLE product_reviews DROP COLUMN IF EXISTS _tmp_noop;

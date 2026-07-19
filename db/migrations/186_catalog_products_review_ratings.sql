-- 186_catalog_products_review_ratings.sql
-- Capture the product REVIEW signal (aggregateRating) into the commerce index.
--
-- WHY: the StyleKorean retailer PDPs (and other schema.org storefronts) expose a
-- JSON-LD `aggregateRating` { ratingValue, reviewCount }, but the ingest path
-- dropped it — reviews were 0% captured across the brand-official canonical
-- cohort. The decision-intelligence lane consumes a product's rating as a
-- first-class decision input, so it needs a durable home on the canonical record
-- and on the served view. These two columns are that home:
--   catalog_products.rating_value  (NUMERIC — e.g. 4.3)
--   catalog_products.rating_count  (INTEGER — e.g. 215 reviews)
-- and they are mirrored onto agent_pdp_view for the serve path.
--
-- CONTRACT: the DI agent reads exactly `rating_value` / `rating_count` (both on
-- catalog_products and agent_pdp_view). Do not rename.
--
-- Additive + nullable: a product with no published reviews stays NULL, existing
-- writers that omit these leave them NULL, and nothing here touches offer pricing
-- (estimated_best_price). A rating is never invented — NULL means "no review data
-- on the source page", not "zero stars".
--
-- Idempotent DDL. Railway prod SKIPS db/migrations/ — the matching self-heal in
-- db/schema_guard.ensure_required_schema_light brings these columns into prod on
-- boot (mirrored per the schema-guard-coverage CI gate).

ALTER TABLE IF EXISTS catalog_products
  ADD COLUMN IF NOT EXISTS rating_value NUMERIC,
  ADD COLUMN IF NOT EXISTS rating_count INTEGER;

ALTER TABLE IF EXISTS agent_pdp_view
  ADD COLUMN IF NOT EXISTS rating_value NUMERIC,
  ADD COLUMN IF NOT EXISTS rating_count INTEGER;

-- Phase O-5b: structured fashion fields on canonical catalog rows.
--
-- The catalog has zero rows carrying material / care / size_guide today
-- (verified 2026-05-17 via direct DB survey). The UI's fashion PDP
-- container (pivota-agent-ui FashionPDPContainer) needs these fields to
-- render real product data instead of curated overlays.
--
-- Per-field provenance + confidence let agents trust-gate the value:
--   merchant_payload  → 1.0 (upstream provided it)
--   variant_aggregate → 0.85 (folded from a variant)
--   regex_extraction_v1 → 0.6–0.9 (heuristic match on description; substring-validated)
--   llm_extraction_v1 → 0.5–0.9 (future; reserved name)
--   manual_review     → 1.0 (admin/employee portal)
--
-- size_guide is JSONB because real size charts are structured rows
-- (Size, Bust, Waist, etc.); material + care are free text.
--
-- All additive + nullable; safe to deploy at any time without backfill.

ALTER TABLE catalog_products
    ADD COLUMN IF NOT EXISTS material TEXT NULL,
    ADD COLUMN IF NOT EXISTS material_source VARCHAR(32) NULL,
    ADD COLUMN IF NOT EXISTS material_confidence REAL NULL,
    ADD COLUMN IF NOT EXISTS care TEXT NULL,
    ADD COLUMN IF NOT EXISTS care_source VARCHAR(32) NULL,
    ADD COLUMN IF NOT EXISTS care_confidence REAL NULL,
    ADD COLUMN IF NOT EXISTS size_guide JSONB NULL,
    ADD COLUMN IF NOT EXISTS size_guide_source VARCHAR(32) NULL,
    ADD COLUMN IF NOT EXISTS size_guide_confidence REAL NULL;

COMMENT ON COLUMN catalog_products.material IS
    'Free-text material composition (e.g. "100% organic cotton"). NULL when unknown.';
COMMENT ON COLUMN catalog_products.material_source IS
    'Provenance enum: merchant_payload | variant_aggregate | regex_extraction_v1 | llm_extraction_v1 | manual_review';
COMMENT ON COLUMN catalog_products.material_confidence IS
    'Float 0.0-1.0; agents should gate merchant-facing prose on >= 0.6 by default.';
COMMENT ON COLUMN catalog_products.care IS
    'Free-text care instructions (e.g. "Machine wash cold; tumble dry low"). NULL when unknown.';
COMMENT ON COLUMN catalog_products.care_source IS
    'Provenance enum (see material_source).';
COMMENT ON COLUMN catalog_products.care_confidence IS
    'Float 0.0-1.0 (see material_confidence).';
COMMENT ON COLUMN catalog_products.size_guide IS
    'Structured size chart JSONB: {columns: [...], rows: [{label, values, stock?}], note?, tip?}.';
COMMENT ON COLUMN catalog_products.size_guide_source IS
    'Provenance enum (see material_source).';
COMMENT ON COLUMN catalog_products.size_guide_confidence IS
    'Float 0.0-1.0 (see material_confidence).';

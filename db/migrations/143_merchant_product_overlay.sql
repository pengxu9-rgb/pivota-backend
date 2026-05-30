-- Migration 143: merchant_product_overlay
-- SKU Optimization OS (hybrid publish path): materialization target of PDP governance publish.
-- When an approved governance module is published, its payload is flattened into per-field
-- overlay rows here. The public PDP merge hook (PIVOTA-Agent
-- enrichProductWithCatalogPdpContentFields) reads the active rows for a product_key at request
-- time and lays them over the served payload -- so a merchant approval reaches the live PDP
-- without mutating the sync-owned catalog_products.product_payload (which is full-overwritten
-- on every sync).
-- Idempotent: safe to re-run (CREATE TABLE / INDEX IF NOT EXISTS).
BEGIN;

CREATE TABLE IF NOT EXISTS merchant_product_overlay (
    overlay_id         TEXT PRIMARY KEY,
    product_key        TEXT NOT NULL,                  -- "merchant_id|platform|source_product_id"
    content_key        TEXT,                           -- canonical, optional convenience join key
    module_key         TEXT NOT NULL,                  -- governance PDP module key (e.g. 'copy')
    field_key          TEXT NOT NULL,                  -- merge-hook field (e.g. 'pdp_description_raw')
    value_jsonb        JSONB NOT NULL,                 -- approved value (scalar or object)
    provenance         TEXT NOT NULL,                  -- merchant_approved | agent_approved | ops_approved
    source_version_id  TEXT,                           -- pdp_module_versions.id this was materialized from
    approval_status    TEXT NOT NULL DEFAULT 'active', -- active | superseded | withdrawn
    approved_by        TEXT,
    approved_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one active overlay per (product_key, module_key, field_key); re-publish supersedes prior.
CREATE UNIQUE INDEX IF NOT EXISTS uq_merchant_product_overlay_active
    ON merchant_product_overlay (product_key, module_key, field_key)
    WHERE approval_status = 'active';

-- Read path: the agent merge hook matches on the merchant_id + source_product_id
-- segments of product_key via split_part, so an expression index on those
-- segments (not the whole product_key) is what actually serves the request-time
-- lookup. Keep the plain product_key index too for writer-side point lookups.
CREATE INDEX IF NOT EXISTS idx_merchant_product_overlay_product_active
    ON merchant_product_overlay (product_key)
    WHERE approval_status = 'active';

CREATE INDEX IF NOT EXISTS idx_merchant_product_overlay_segments_active
    ON merchant_product_overlay (
        split_part(product_key, '|', 1),
        split_part(product_key, '|', 3)
    )
    WHERE approval_status = 'active';

COMMIT;

-- 085_agent_pdp_view.sql
--
-- Stage 3a-i of the PDP architecture roadmap (plans/rosy-mixing-bengio.md).
-- Creates the denormalized materialized table the agent-facing chat
-- surface reads instead of the join-heavy get_pdp_v2 gateway operation.
--
-- Why this table exists:
--
-- The agent.pivota.cc PDP currently fetches get_pdp_v2 twice per render
-- (generateMetadata + page component). Each call is a 200-700ms POST
-- to the Railway-hosted backend, where the gateway joins
-- catalog_products × catalog_skus × catalog_offers × product_group_members
-- × subject_resolve plus a 3-step fallback resolution chain. Total
-- cold-load TTFB measured at 200-700ms on 2026-05-12.
--
-- Stage 3a-i: add a single denormalized row per canonical product
-- with everything the PDP needs pre-joined. Stage 3a-ii will
-- backfill it; Stage 3a-iii wires writer-service refresh; Stage 3a-iv
-- ships the new /api/agent/pdp/{id} endpoint that reads this table
-- (target: <10ms p99 SELECT).
--
-- Index strategy:
--   content_key   PRIMARY KEY (the canonical product identity, sha256-
--                              derived in Stage 1)
--   pivota_signature_id  UNIQUE partial — for /products/sig_* lookups
--   product_group_id     partial — for multi-merchant group lookups
--   gtin13               partial — cross-merchant identity probe
--   brand                non-unique — "find products by brand" queries
--
-- JSONB columns are kept small (top-N offers, ~50 variants max) so
-- the whole row fits comfortably in one PG page. Cap counts in the
-- backfill script (3a-ii), not at the schema layer — flexibility
-- for late-cycle tuning.
--
-- Idempotent: CREATE TABLE / INDEX with IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS agent_pdp_view (
  -- Identity (Stage 1 + 2b-i derivations)
  content_key VARCHAR(40) PRIMARY KEY,
  pivota_signature_id VARCHAR(40),
  product_group_id VARCHAR(64),

  -- Core content (denormalized from catalog_products + external_product_seeds.seed_data)
  brand TEXT,
  title TEXT NOT NULL,
  description TEXT,
  image_url TEXT,
  image_urls JSONB,

  -- Price + offer aggregation (denormalized from catalog_offers grouped by product_group)
  currency VARCHAR(3),
  price_min NUMERIC(12, 2),
  price_max NUMERIC(12, 2),
  offer_count INT,
  -- Top-N offers as [{merchant_id, merchant_name, price, currency, availability, url}].
  -- Bounded to a reasonable cap by the backfill (Stage 3a-ii) — typically <=5
  -- since most products have a handful of sellers. AggregateOffer/Schema.org
  -- emission (frontend Stage 3b-2) reads this directly.
  offers JSONB,

  -- Variants (denormalized from catalog_skus where source_variant_id != canonical)
  -- as [{variant_id, sku, title, options, image_url, price, currency, availability}].
  -- Bounded to ~50 entries (Tom Ford foundation has 40 shades — the largest
  -- single-product variant count we've seen in prod).
  variants JSONB,
  variants_count INT,

  -- GTIN-13 — when set on any catalog_skus row under this content_key.
  -- Stripped of non-digits + zero-padded to 14 (GS1 canonical form, same
  -- normalization as services/catalog_identity.normalize_gtin).
  gtin13 VARCHAR(14),

  -- Taxonomy + classification (Stage O-2 derivations)
  category_path TEXT,
  taxonomy_tags JSONB,
  -- Pre-built breadcrumb [{position, name, item}] so the frontend doesn't
  -- recompute on every render. Mirrors Stage 3b-1's productJsonLd.ts shape.
  breadcrumb JSONB,

  -- Lifecycle + sync state
  pdp_lifecycle_stage VARCHAR(16),
  sync_status VARCHAR(16),
  primary_merchant_id VARCHAR(64),

  -- Refresh metadata
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- When this row was refreshed by the seed_data writer (Stage 3a-iii),
  -- link to the proposal that triggered the refresh — full audit trail
  -- from "operator wrote seed_data" → "agent_pdp_view rebuilt".
  refreshed_by_proposal_id BIGINT,
  -- Free-form provenance for refreshes that didn't come from a proposal
  -- (initial backfill, scheduled refresh, etc.).
  refresh_source TEXT
);

COMMENT ON TABLE agent_pdp_view IS
  'Denormalized one-row-per-canonical-product view of the catalog. Read path for /api/agent/pdp/{id} (Stage 3a-iv). Refreshed by Stage 3a-iii hook on seed_data writer commits; backfilled by Stage 3a-ii one-shot script. See plans/rosy-mixing-bengio.md Stage 3a.';

COMMENT ON COLUMN agent_pdp_view.content_key IS
  'Primary key — canonical product identity derived in Stage 1 (services.catalog_identity.make_content_key).';

COMMENT ON COLUMN agent_pdp_view.offers IS
  'Top-N offers across merchants for this canonical product. Bounded by the backfill (Stage 3a-ii) to keep row size bounded. Schema: [{merchant_id, merchant_name, price, currency, availability, url, is_primary}].';

COMMENT ON COLUMN agent_pdp_view.variants IS
  'Variant SKUs (Stage 2b-ii promoter output) flattened to a single jsonb array. Schema: [{variant_id, sku, title, options: {shade?, size?, color?}, image_url, price, currency}].';

COMMENT ON COLUMN agent_pdp_view.breadcrumb IS
  'Pre-built breadcrumb [{position, name, item}] — frontend renders verbatim into schema.org BreadcrumbList without recomputation.';


-- ---------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------

-- Pivota signature lookup (most-common single-product GET path)
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_pdp_view_pivota_signature_id
  ON agent_pdp_view (pivota_signature_id)
  WHERE pivota_signature_id IS NOT NULL;

-- Product group lookup (multi-merchant card rendering)
CREATE INDEX IF NOT EXISTS idx_agent_pdp_view_product_group_id
  ON agent_pdp_view (product_group_id)
  WHERE product_group_id IS NOT NULL;

-- GTIN cross-merchant identity probe
CREATE INDEX IF NOT EXISTS idx_agent_pdp_view_gtin13
  ON agent_pdp_view (gtin13)
  WHERE gtin13 IS NOT NULL;

-- Brand-scoped queries ("find Tom Ford products")
CREATE INDEX IF NOT EXISTS idx_agent_pdp_view_brand
  ON agent_pdp_view (brand)
  WHERE brand IS NOT NULL;

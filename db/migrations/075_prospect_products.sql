-- BD cold-start audit: prospect_products table.
--
-- Every BD cold-start audit discovers products from a target brand's
-- website (catalog-intelligence primary path; brand_product_discovery
-- fallback). The discovered products land here as PROSPECT data —
-- separate from catalog_products (the real merchant catalog).
--
-- Why a separate table?
--   1. Cold-target data is tentative. The brand may never onboard.
--      Mixing it into catalog_products would conflate prospect SKUs
--      with real merchant SKUs.
--   2. When a brand DOES onboard, we want to "claim" their prospect
--      rows — link them to the new merchant_id. claimed_at +
--      claimed_by_merchant_id support that flow.
--   3. Audit history: every BD audit run is recorded against the
--      prospect; comparing audit results across cold-call cohorts
--      becomes possible later (e.g. "of 100 prospects we audited,
--      which onboarded?").
--
-- The full extracted payload is preserved as raw_extracted JSONB so
-- catalog-intelligence's rich data (variants, pricing, reviews,
-- ingredients) is available for re-use without re-crawling.

CREATE TABLE IF NOT EXISTS prospect_products (
  prospect_brand    TEXT NOT NULL,
  prospect_domain   TEXT NOT NULL,
  url               TEXT NOT NULL,
  title             TEXT NULL,
  vendor            TEXT NULL,
  product_type      TEXT NULL,
  -- Where this row came from. catalog_intelligence is the rich path;
  -- brand_product_discovery_* are the lightweight fallbacks. Useful
  -- for diagnostics when an audit produces weak signals.
  discovery_source  TEXT NOT NULL,
  -- Full payload from the discovery source (catalog-intelligence's
  -- ExtractedProduct shape; for fallback paths, just the minimal
  -- title + url). Preserves the rich data without forcing a schema
  -- migration every time catalog-intelligence adds a field.
  raw_extracted     JSONB NULL,
  discovered_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- Most recent BD audit run that touched this prospect (for trend
  -- analysis once we have multiple audits per prospect).
  last_audit_run_id UUID NULL,
  last_audited_at   TIMESTAMPTZ NULL,
  -- When the brand later onboards as a real merchant, claim flow
  -- sets these. Re-keys this row to a real merchant_id.
  claimed_at                 TIMESTAMPTZ NULL,
  claimed_by_merchant_id     TEXT NULL,
  PRIMARY KEY (prospect_domain, url)
);

-- "Show me all prospects discovered from <domain>" query.
CREATE INDEX IF NOT EXISTS idx_prospect_products_domain_discovered
  ON prospect_products (prospect_domain, discovered_at DESC);

-- "Show me unclaimed prospects" — feeds onboarding outreach lists.
CREATE INDEX IF NOT EXISTS idx_prospect_products_unclaimed
  ON prospect_products (claimed_at)
  WHERE claimed_at IS NULL;

-- Migration 097: brand_display_names — ingest-time canonical brand caching.
--
-- Raw brand strings from product.vendor (Shopify) are often inconsistently
-- cased or stylized (e.g. "nike" vs "NIKE" vs "Nike"). This table caches the
-- LLM-resolved canonical display form so the resolution cost is paid exactly
-- once per unique raw_name. Subsequent ingest hits are pure SELECT.
--
-- source enum:
--   'llm'    — resolved by DeepSeek at ingest time (default)
--   'manual' — overridden by ops/merchants directly
--   'crawl'  — resolved from an upstream brand registry crawl (future)
--
-- Lookups always use LOWER(raw_name) to tolerate minor case drift between
-- ingest batches without producing duplicate rows.

CREATE TABLE IF NOT EXISTS brand_display_names (
    raw_name        VARCHAR(255) PRIMARY KEY,
    display_name    VARCHAR(255) NOT NULL,
    confidence      FLOAT        NOT NULL,
    source          VARCHAR(32)  NOT NULL DEFAULT 'llm',
    resolved_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Case-insensitive lookup index. raw_name PRIMARY KEY only gives an exact
-- match; queries that normalise with LOWER() need this index to stay O(log n).
CREATE INDEX IF NOT EXISTS idx_brand_display_names_lower_raw
    ON brand_display_names (LOWER(raw_name));

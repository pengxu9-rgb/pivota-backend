-- 071_pivota_canonical_pdp.sql
--
-- Canonical Pivota PDP: every onboarded merchant product gets a stable
-- agent.pivota.cc/products/sig_* URL — the AI-channel surface that
-- LLM grounded retrieval (Gemini/ChatGPT/Perplexity) cites and that
-- agent-direct API consumers (apps-as-agents, personal agents) resolve
-- against the agentic-commerce protocol.
--
-- Until now, only 6 hardcoded seeds in pivota-agent-ui/src/app/sitemap-
-- seeds.ts had canonical PDPs. This migration extends the model so any
-- merchant product gets one — populated lazily at audit time and
-- eagerly at catalog-sync time.
--
--   pivota_signature_id   sig_<32-hex> — deterministic from
--                         (merchant_id, platform, source_product_id).
--                         Stable across syncs; safe to publish in
--                         sitemaps + sitemap submissions.
--   pivota_canonical_url  https://agent.pivota.cc/products/<sig_id>
--                         (or whatever CHECKOUT_UI_BASE_URL points to).
--                         Denormalized for fast read; could be derived
--                         from sig_id but we keep the literal column
--                         so a single SELECT loads everything the
--                         audit + sitemap need.
--
-- Both columns are nullable for now — the catalog has rows that
-- predate this migration and will be populated by the next sync OR
-- the first audit that touches them. NOT NULL constraint is a future
-- migration once backfill completes.

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS pivota_signature_id TEXT NULL,
  ADD COLUMN IF NOT EXISTS pivota_canonical_url TEXT NULL;

-- Look up by signature for the canonical PDP renderer (sig → product).
-- Unique because sig is deterministic per product.
CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_products_pivota_signature
  ON catalog_products (pivota_signature_id)
  WHERE pivota_signature_id IS NOT NULL;

COMMENT ON COLUMN catalog_products.pivota_signature_id IS
  'sig_<32hex> — deterministic from (merchant_id, platform, source_product_id). Identifies the canonical Pivota PDP at agent.pivota.cc/products/<sig>. See services/catalog_sync_service.py:make_pivota_signature_id';

COMMENT ON COLUMN catalog_products.pivota_canonical_url IS
  'Full https://agent.pivota.cc/products/<sig_id> URL. Denormalized for read-path simplicity. Populated by catalog sync going forward + lazily at audit time for older rows.';

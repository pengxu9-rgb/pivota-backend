-- 248: storefront proof that an ENRICHMENT catalog sku names a live Shopify variant (db/enrichment_cart_variant_proofs.py).
--
-- Option 2, PR A (2026-09-29). The Reap cart-link lane can buy a catalog_enrichment_agent_v1 row only when a
-- fresh read of the brand's own storefront shows the sku's variant is live there. One row per catalog
-- (product_key, sku_key): the host and handle the proof was read from, the Shopify product and variant it
-- found, how many variants that handle has (ALL of them, sold out or not), and the variant's availability
-- and price at checked_at. `outcome` is 'ok' only for a proof the purchase may rely on; any other value
-- ('revoked_404', 'variant_gone', 'host_redirected', ...) is kept so the job can tell "gone" from "never
-- checked". The writer's full contract is the "THE PROOF CONTRACT" section of
-- services/reap_enrichment_cart_proof.py.
--
-- The verifier is services/reap_enrichment_cart_proof.verify_enrichment_cart_proof. Nothing reads or writes
-- this table yet: the writer (jobs/enrichment_cart_variant_proof.py) is PR B and the purchase lane that reads
-- it is PR C.
--
-- product_key and sku_key are TEXT, not VARCHAR(128): an enrichment product_key reaches 214 characters
-- (services/catalog_enrichment_agent/ingestion.derive_product_key) and its sku_key 255.
-- live_price_minor is in the ISO-4217 minor unit of `currency` (cents for USD/SGD, yen for JPY).
--
-- Migrations do not self-apply in prod; ensure_table() in db/enrichment_cart_variant_proofs.py runs the
-- identical CREATE at first use. This file is the record, and what the SQL gates plan against.

CREATE TABLE IF NOT EXISTS enrichment_cart_variant_proofs (
  product_key         TEXT NOT NULL,
  sku_key             TEXT NOT NULL,
  shop_host           TEXT NOT NULL,
  handle              TEXT NOT NULL,
  shopify_product_id  TEXT,
  variant_id          TEXT,
  live_variant_count  INTEGER CHECK (live_variant_count IS NULL OR live_variant_count >= 0),
  available           BOOLEAN,
  live_price_minor    BIGINT CHECK (live_price_minor IS NULL OR live_price_minor >= 0),
  currency            TEXT CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
  source              TEXT NOT NULL,
  checked_at          TIMESTAMPTZ NOT NULL,
  outcome             TEXT NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT ck_enrichment_cart_variant_proofs_source CHECK (
    source IN ('products_json_v1', 'products_js_v1')
  ),
  CONSTRAINT ck_enrichment_cart_variant_proofs_ok_has_evidence CHECK (
    outcome <> 'ok' OR (
      variant_id IS NOT NULL AND available IS NOT NULL AND currency IS NOT NULL
      AND live_variant_count IS NOT NULL AND live_variant_count >= 1
      AND live_price_minor IS NOT NULL AND live_price_minor > 0
    )
  ),
  PRIMARY KEY (product_key, sku_key)
);

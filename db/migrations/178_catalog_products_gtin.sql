-- 178_catalog_products_gtin.sql
-- ADR-011 (intake identity contract) — GTIN as a MATCH ATTRIBUTE, not key-material.
--
-- WHY: content_key is the merchant-agnostic brand+title FAMILY identity. The
-- earlier design folded GTIN into that key (make_content_key(brand,title,gtin)),
-- which meant a product seen WITH a barcode and the same product seen WITHOUT
-- one minted two parallel identities — the exact crawl-scale fragmentation the
-- contract exists to stop. The SPU model (Amazon ASIN / Dewu SPU) keeps ONE
-- internal family identity and treats GTIN as a strong cross-merchant MATCHER.
-- This column is where that match attribute lives: the resolve-or-attach
-- primitive (services/intake_identity.py) Tier-0 GTIN lookup keys on it, and
-- every intake door persists the source barcode here (GS1-canonical GTIN-14,
-- via services.catalog_identity.normalize_gtin).
--
-- Additive + nullable: existing writers that omit it leave it NULL, so behavior
-- is byte-identical for identity/serving. Legacy rows stay NULL until a
-- re-ingest populates them (or a D-2-adjacent backfill lands).
--
-- Idempotent DDL. Railway prod SKIPS db/migrations/ — the matching self-heal in
-- db/schema_guard.ensure_required_schema_light brings this column + index into
-- prod on boot.

ALTER TABLE IF EXISTS catalog_products
  ADD COLUMN IF NOT EXISTS gtin TEXT;

CREATE INDEX IF NOT EXISTS idx_catalog_products_gtin
  ON catalog_products (gtin)
  WHERE gtin IS NOT NULL;

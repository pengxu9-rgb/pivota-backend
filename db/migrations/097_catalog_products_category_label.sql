-- Migration 097: add category_label to catalog_products
-- Codex applied this directly to prod (2026-05-20) to unblock the LLM backfill;
-- this migration codifies it so dev/staging environments stay in sync.
ALTER TABLE catalog_products
    ADD COLUMN IF NOT EXISTS category_label VARCHAR(255);

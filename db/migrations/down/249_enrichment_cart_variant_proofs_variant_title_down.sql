-- Reverses 249_enrichment_cart_variant_proofs_variant_title.sql. The column is display-only; dropping it
-- loses the recorded titles and nothing else. NOT STICKY: db/enrichment_cart_variant_proofs.ensure_table()
-- and db/schema_guard.py re-add it, so remove both in the same release if it must stay gone.
ALTER TABLE enrichment_cart_variant_proofs DROP COLUMN IF EXISTS variant_title;

-- Migration 174: durable catalog_products.llm_attributes
-- (multi-vertical audit, Phase 2b-2 — extractor cache).
--
-- The LLM attribute extractor (services/llm_attribute_extractor.py) is a
-- once-per-SKU cost. Cache its grounded output on the product row so re-audits
-- don't re-pay the LLM. Payload shape:
--   { "source_hash": "<fingerprint of the product copy the guard checked>",
--     "attributes": [ {"class_name","value","span"}, ... ] }
-- The source_hash invalidates the cache when the product copy changes, so a stale
-- cache can never seed a probe for a spec the (edited) page no longer states.
-- Populated go-forward by the audit's context-build write path; no backfill.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.
BEGIN;

ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS llm_attributes JSONB;

COMMIT;

-- 177_intake_identity_events.sql
-- ADR-011 (intake identity contract): provenance for every resolve-or-attach
-- outcome at every intake door. Each row is one {door, action, matcher,
-- evidence} decision made by services/intake_identity.resolve_or_attach_
-- content_identity BEFORE a catalog_products insert — feeding ADR-010's D-2
-- reconciliation schema and gold-label capture.
--
--   door     → which of the five chokepoints (catalog_sync, external_seed_
--              mirror, brand_authored, catalog_enrichment, url_audit_intake).
--   action   → ATTACH | MINT | FLAG | SKIP.
--   matcher  → the Tier-0 matcher that decided (content_key_gtin,
--              content_key_brand_title[_gtin_fallback], canonical_url_match,
--              source_product_id_match, gtin_disagreement, gtin_conflict,
--              brand_host_fragmentation; NULL for plain MINT).
--   evidence → the full input + match detail JSON.
--
-- Best-effort sink: the writer never blocks intake on a provenance failure.
-- Idempotent DDL. Railway prod SKIPS db/migrations/ — the matching self-heal
-- in db/schema_guard.ensure_required_schema_light creates this table on boot.

CREATE TABLE IF NOT EXISTS intake_identity_events (
  id                BIGSERIAL PRIMARY KEY,
  door              TEXT NOT NULL,
  action            TEXT NOT NULL,
  matcher           TEXT NULL,
  merchant_id       TEXT NULL,
  product_key       TEXT NULL,
  content_key       TEXT NULL,
  product_group_id  TEXT NULL,
  evidence          JSONB NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_intake_identity_events_content_key
  ON intake_identity_events (content_key)
  WHERE content_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_intake_identity_events_door_action
  ON intake_identity_events (door, action, created_at);

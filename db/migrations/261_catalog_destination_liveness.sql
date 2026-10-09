-- 261_catalog_destination_liveness.sql
--
-- Is a catalog product's storefront page still there? For the rows that have no seed to ask.
--
-- `external_product_seeds` carries destination liveness (migration 200) for the seed mirror, and
-- jobs.external_seed_destination_sweep re-reads it nightly. The catalog enrichment lane
-- (`catalog_products.source_system = 'catalog_enrichment_agent_v1'`) has no seed row, so nothing
-- ever re-read its pages: on 2026-10-09 it served 12,944 in-stock storefront rows, and a
-- throttled live sample found 9 of 150 (6%) on a page the store answered 404 for.
--
-- One row per catalog product, written ONLY by services/catalog_destination_liveness (the
-- nightly jobs.catalog_destination_sweep). The verdict vocabulary, the failure streak and its
-- clock mean exactly what they mean on external_product_seeds (migrations 200 and 260), and the
-- same pure function computes them.
--
-- Two clocks, as the refresh queue keeps them (migration 202):
--   destination_checked_at  stamped only by an observation that REACHED the origin;
--   last_attempt_at         stamped by every attempt, so a host we cannot read does not pin the
--                           head of the queue every night.
--
-- Applied by the job itself from this file (idempotent), so the table exists wherever the job
-- runs; nothing else reads it.

CREATE TABLE IF NOT EXISTS catalog_destination_liveness (
  product_key TEXT PRIMARY KEY,
  canonical_url TEXT NOT NULL,
  host TEXT NULL,
  destination_verdict TEXT NULL
    CONSTRAINT ck_catalog_destination_liveness_verdict CHECK (
      destination_verdict IS NULL
      OR destination_verdict IN (
        'live',
        'live_delisted',
        'redirected_to_product',
        'redirected_off_product',
        'dead_404',
        'unverifiable'
      )
    ),
  destination_http_status INTEGER NULL,
  destination_failure_streak INTEGER NOT NULL DEFAULT 0,
  destination_corroborated_dead_at TIMESTAMPTZ NULL,
  destination_checked_at TIMESTAMPTZ NULL,
  last_attempt_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The sweep queue's second key (the first is "dead first", a predicate on the verdict).
CREATE INDEX IF NOT EXISTS idx_catalog_destination_liveness_last_attempt
  ON catalog_destination_liveness (last_attempt_at NULLS FIRST);

CREATE INDEX IF NOT EXISTS idx_catalog_destination_liveness_dead
  ON catalog_destination_liveness (destination_verdict)
  WHERE destination_verdict IN ('dead_404', 'redirected_off_product');

COMMENT ON TABLE catalog_destination_liveness IS
  'Storefront page liveness for catalog rows with no seed (catalog_enrichment_agent_v1). Written only by services/catalog_destination_liveness.';

-- Rollback (manual):
-- DROP TABLE IF EXISTS catalog_destination_liveness;

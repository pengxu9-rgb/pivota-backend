-- 159_citation_observations.sql
-- P0.2 (commerce-index keystone): persist the cross-channel CITATION MATRIX
-- relationally. Every audit computes build_authority_map.hosts[] — the
-- per-host x per-query x per-provider matrix of who got cited, on which
-- channel, by which model, for which query (first-party vs competitor) — and
-- then THROWS IT AWAY: it survives only inside merchant_audit_runs.report_jsonb,
-- and the per-query linkage is pop()'d in
-- services/agent_center_bd_report_service.py (build_authority_map, the
-- `_queries` pop). That discarded structure is simultaneously:
--   (a) the longitudinal cross-model visibility dataset no incumbent collects,
--   (b) the merit-ranking signal, and
--   (c) the proof-loop spine.
--
-- This table is its durable, queryable home, keyed on content_key so
-- observations accrete on the canonical entity across sellers. Rows are
-- written ONLY for depositable (resolved) content_keys — see
-- services.catalog_identity.resolve_deposit_content_key — so the deliberately
-- non-unique content_key (migration 083) can't cross-contaminate entities.
--
-- One row per (audit_run, content_key, provider, query, cited_host).
-- idempotency_key dedupes re-runs of the same audit.
--
-- Idempotent. Prod skips the migration runner — apply via railway ssh / admin.
BEGIN;

CREATE TABLE IF NOT EXISTS citation_observations (
    observation_id    UUID PRIMARY KEY,
    audit_run_id      TEXT NOT NULL,        -- merchant_audit_runs.run_id (string form)
    merchant_id       TEXT NOT NULL,
    content_key       VARCHAR(40) NOT NULL,
    product_key       TEXT NULL,
    provider          TEXT NOT NULL,        -- gemini | chatgpt | perplexity | ...
    query             TEXT NOT NULL,
    axis              TEXT NULL,            -- the load-bearing query axis
    query_class       TEXT NULL,
    cited_host        TEXT NULL,
    host_type         TEXT NULL,            -- retailer | marketplace | editorial | brand | ...
    citation_role     TEXT NULL,            -- findability | endorsement | ...
    first_party       BOOLEAN NULL,
    is_competitor     BOOLEAN NULL,
    evidence_url      TEXT NULL,            -- the destination URL on cited_host
    content_key_basis TEXT NOT NULL,        -- gtin | identity_high_conf | reviewed
    observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    idempotency_key   TEXT NOT NULL         -- hash(run, content_key, provider, query, cited_host)
);

-- Dedupe re-runs of the same audit (one logical observation = one row).
CREATE UNIQUE INDEX IF NOT EXISTS ux_citation_observations_idem
  ON citation_observations (idempotency_key);

-- Entity-centric read: how does THIS product get cited, by provider, over time.
CREATE INDEX IF NOT EXISTS idx_citation_observations_entity
  ON citation_observations (content_key, provider, observed_at DESC);

-- Per-run read (deposit verification / report_jsonb parity checks).
CREATE INDEX IF NOT EXISTS idx_citation_observations_run
  ON citation_observations (audit_run_id);

-- Per-merchant longitudinal read.
CREATE INDEX IF NOT EXISTS idx_citation_observations_merchant
  ON citation_observations (merchant_id, observed_at DESC);

COMMIT;

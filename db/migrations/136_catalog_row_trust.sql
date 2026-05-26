-- 136_catalog_row_trust.sql
--
-- Layer C1: denormalized row-level trust contract.
-- One row-shape readers consume instead of re-implementing predicates over
-- catalog_products / catalog_offers / pdp_identity_listing /
-- index_pipeline_state / catalog_source_quarantine / external_product_seeds /
-- merchant_stores.
--
-- Design: project_identity_trust_architecture_audit (audit 2026-05-25),
-- Layer C Option C1, minimum viable contract.
--
-- This migration creates the table and indexes only. The contract is owned
-- by services/catalog_trust_policy (Python) and src/services/catalogTrustPolicy.js
-- (Node) — those modules compute (subject_type, subject_key) → trust row.
--
-- Phase 1 (this migration + policy v0): backfill via one-shot job;
-- dual-write integration is Phase 2 (see services/catalogTrustPolicy.js).
-- No readers are migrated by this PR.
--
-- schema-guard-exempt: creates a new opt-in table; no runtime ADD COLUMN
-- self-heal needed.

CREATE TABLE IF NOT EXISTS catalog_row_trust (
  -- Identity of the subject row
  subject_type    TEXT NOT NULL CHECK (subject_type IN ('product','offer','listing','content_key')),
  subject_key     TEXT NOT NULL,

  -- Cross-references to the source rows the trust decision was derived from.
  -- All nullable: not every subject_type has every reference.
  product_key                       TEXT NULL,
  source_listing_ref                TEXT NULL,   -- external_product_seeds.id or merchant SKU pointer
  content_key                       TEXT NULL,
  source_id                         TEXT NULL,   -- forward-compat with Layer A1 source registry
  source_domain                     TEXT NULL,   -- shipped by migration 133

  -- Source lifecycle (Layer A)
  source_lifecycle_state            TEXT NOT NULL CHECK (source_lifecycle_state IN (
    'active','suspect','inactive','quarantined','tombstoned','unknown'
  )),
  source_last_checked_at            TIMESTAMPTZ NULL,

  -- Identity layer (Layer B)
  identity_status                   TEXT NOT NULL CHECK (identity_status IN (
    'approved','review_required','conflict','unknown'
  )),
  identity_confidence               NUMERIC(4,3) NULL CHECK (
    identity_confidence IS NULL OR (identity_confidence >= 0 AND identity_confidence <= 1)
  ),
  matched_product_key               TEXT NULL,
  matched_content_key               TEXT NULL,
  matched_sellable_item_group_id    TEXT NULL,

  -- Freshness
  freshness_state                   TEXT NOT NULL CHECK (freshness_state IN (
    'fresh','stale','expired','unverified'
  )),
  last_verified_at                  TIMESTAMPTZ NULL,
  verification_source               TEXT NULL,   -- shopify_sync | external_seed_scrape | manual_review | identity_resolver | source_healthcheck

  -- Reader contract: the single decision every reader must respect.
  -- Public catalog readers may serve only serving_decision='public'.
  -- Recommendation/dedup readers may use identity_confidence + matched_* for ranking.
  -- Internal/debug readers may include 'shadow' but must expose reason_codes.
  serving_decision                  TEXT NOT NULL CHECK (serving_decision IN (
    'public','shadow','blocked'
  )),
  serving_reason_codes              TEXT[] NOT NULL DEFAULT '{}',

  -- Audit + versioning
  manual_override_id                TEXT NULL,
  policy_version                    TEXT NOT NULL,
  updated_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),

  PRIMARY KEY (subject_type, subject_key)
);

COMMENT ON TABLE catalog_row_trust IS
  'Layer C1 denormalized trust contract. One row per (subject_type, subject_key). '
  'Readers depend on this shape (esp. serving_decision + serving_reason_codes) '
  'instead of re-implementing trust predicates over source/identity/IPS/suppression tables. '
  'Produced by catalogTrustPolicy (Node) / catalog_trust_policy (Python).';

COMMENT ON COLUMN catalog_row_trust.serving_decision IS
  'public | shadow | blocked. Public-facing readers MUST gate on public only. '
  'Shadow surfaces in internal/debug tools and is the policy''s answer for '
  'rows that would be served today but fail the contract — used during cutover.';

COMMENT ON COLUMN catalog_row_trust.serving_reason_codes IS
  'Bounded vocabulary; see catalogTrustPolicy.REASON_CODES for the authoritative list. '
  'Always populated for shadow/blocked; may be empty for public.';

COMMENT ON COLUMN catalog_row_trust.policy_version IS
  'Semver-ish string emitted by the policy module. Bump on any change to '
  'serving_decision derivation logic so backfills can be detected as stale.';

-- Reader query shapes:
--   (1) "give me everything servable" → WHERE serving_decision='public'
--   (2) "diagnose why X is/isn''t served" → WHERE subject_key=$1
--   (3) "what got blocked today by reason R" → WHERE 'R' = ANY(serving_reason_codes)
--   (4) "what changed under policy vN" → WHERE policy_version <> 'vN'

CREATE INDEX IF NOT EXISTS idx_crt_serving_decision
  ON catalog_row_trust (serving_decision)
  WHERE serving_decision = 'public';

CREATE INDEX IF NOT EXISTS idx_crt_source_lifecycle_state
  ON catalog_row_trust (source_lifecycle_state)
  WHERE source_lifecycle_state <> 'active';

CREATE INDEX IF NOT EXISTS idx_crt_identity_status
  ON catalog_row_trust (identity_status)
  WHERE identity_status <> 'approved';

CREATE INDEX IF NOT EXISTS idx_crt_product_key
  ON catalog_row_trust (product_key)
  WHERE product_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_crt_source_listing_ref
  ON catalog_row_trust (source_listing_ref)
  WHERE source_listing_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_crt_policy_version_updated
  ON catalog_row_trust (policy_version, updated_at);

CREATE INDEX IF NOT EXISTS idx_crt_reason_codes_gin
  ON catalog_row_trust USING GIN (serving_reason_codes);

-- DOWN migration: db/migrations/down/136_catalog_row_trust_down.sql

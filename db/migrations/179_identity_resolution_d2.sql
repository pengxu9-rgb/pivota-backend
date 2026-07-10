-- 179_identity_resolution_d2.sql
--
-- ADR-010 action item 3 (the "D-2" schema increment), per
-- docs/plans/adr010_d2_catalog_reconciliation_at_scale.md Phase A1.
--
-- Turns catalog reconciliation from hand-cut lane scripts (step-5,
-- docs/plans/adr011_step5_catalog_identity_reconciliation.md) into a
-- propose -> review -> apply pipeline with durable provenance and a
-- first-class unmerge path. Additive only; no behavior change until the
-- Phase A2 engine writes here. Railway deploys skip db/migrations/, so
-- db/schema_guard.py mirrors every statement (self-heal at boot).
--
-- 1. identity_resolution_proposals — one row per proposed reconciliation
--    action. Lives OUTSIDE product_group_members because that table's
--    PK (merchant_id, platform, platform_product_id) structurally forbids
--    competing proposals for the same subject (ADR-010 line 30).
--    proposal_key dedupes re-proposals of the same (strategy, subject,
--    member-set) across sweep runs; a changed member set = a new key.
--
-- 2. identity_resolution_events — append-only audit of every apply/revert,
--    superset of what step-5 stored per-row in suppression_metadata.
--    detail JSONB carries the deactivated seed ids so revert can restore
--    them (the lane-2 keeper-orphan incident made this a requirement).
--
-- 3. product_group_members provenance — ADR-010's exact column list
--    {match_tier, confidence, evidence, resolver_version, resolved_at}.
--    Nullable; legacy memberships stay NULL until a resolver touches them.
--
-- 4. pdp_review_tasks — previously defined only in db/pdp_governance.py
--    (SQLAlchemy metadata); ADR-010 D-2 asks for it in migrations. Shape
--    mirrors the ORM definition; CREATE TABLE IF NOT EXISTS is a no-op
--    everywhere the table already exists.

BEGIN;

CREATE TABLE IF NOT EXISTS identity_resolution_proposals (
  proposal_id          TEXT PRIMARY KEY,
  proposal_key         TEXT NOT NULL,
  kind                 TEXT NOT NULL,  -- suppress_dup | flip_canonical | attach_membership | unmerge | label_only
  strategy             TEXT NOT NULL,  -- same_url_dup | campaign_clone | seed_first_party_twin | junk_url | multi_seller_observation | ...
  resolver_version     TEXT NOT NULL,
  merchant_id          TEXT NULL,
  content_key          TEXT NULL,
  subject_product_keys TEXT[] NOT NULL,
  keeper_product_key   TEXT NULL,      -- NULL for label_only / unmerge kinds
  member_fingerprint   TEXT NOT NULL,  -- drift guard: sorted member-set hash at propose time
  confidence           NUMERIC NULL,
  evidence             JSONB NULL,
  status               TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | applied | rejected | reverted
  run_id               TEXT NULL,      -- set at apply time
  decided_by           TEXT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  decided_at           TIMESTAMPTZ NULL,
  applied_at           TIMESTAMPTZ NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_resolution_proposals_key
  ON identity_resolution_proposals (proposal_key);
CREATE INDEX IF NOT EXISTS idx_identity_resolution_proposals_status
  ON identity_resolution_proposals (status, strategy);
CREATE INDEX IF NOT EXISTS idx_identity_resolution_proposals_content_key
  ON identity_resolution_proposals (content_key)
  WHERE content_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS identity_resolution_events (
  id           BIGSERIAL PRIMARY KEY,
  proposal_id  TEXT NULL,   -- NULL for pre-D2 backfilled actions (step-5 lanes)
  action       TEXT NOT NULL,  -- applied | reverted
  run_id       TEXT NOT NULL,
  detail       JSONB NULL,     -- rows touched, deactivated seed ids, post-check results
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_identity_resolution_events_run
  ON identity_resolution_events (run_id);
CREATE INDEX IF NOT EXISTS idx_identity_resolution_events_proposal
  ON identity_resolution_events (proposal_id)
  WHERE proposal_id IS NOT NULL;

ALTER TABLE IF EXISTS product_group_members
  ADD COLUMN IF NOT EXISTS match_tier TEXT NULL,
  ADD COLUMN IF NOT EXISTS confidence NUMERIC NULL,
  ADD COLUMN IF NOT EXISTS evidence JSONB NULL,
  ADD COLUMN IF NOT EXISTS resolver_version TEXT NULL,
  ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ NULL;

CREATE TABLE IF NOT EXISTS pdp_review_tasks (
  id                    VARCHAR(96) PRIMARY KEY,
  pdp_id                VARCHAR(96) NOT NULL,
  module_key            VARCHAR(40) NOT NULL,
  version_id            VARCHAR(96) NULL,
  status                VARCHAR(32) NOT NULL DEFAULT 'needs_review',
  assignee_actor_id     VARCHAR(128) NULL,
  assignee_role         VARCHAR(64) NULL,
  priority              VARCHAR(24) NOT NULL DEFAULT 'normal',
  qa_sample             BOOLEAN NOT NULL DEFAULT FALSE,
  checklist             JSONB NULL,
  policy_labels         JSONB NULL,
  decision_tree_path    JSONB NULL,
  escalation_reason     TEXT NULL,
  override_reason       TEXT NULL,
  review_duration_ms    INTEGER NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at           TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_pdp_review_tasks_lookup
  ON pdp_review_tasks (pdp_id, module_key, version_id);
CREATE INDEX IF NOT EXISTS idx_pdp_review_tasks_status_updated
  ON pdp_review_tasks (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pdp_review_tasks_assignee
  ON pdp_review_tasks (assignee_actor_id, status);

COMMIT;

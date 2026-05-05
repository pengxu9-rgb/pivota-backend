-- Migration 067: Agent Center V1 — shared runtime data model
-- Created: 2026-05-05
-- Purpose: foundation for the 5 merchant-facing agents
--   (Demand Test → SKU Match → Offer Execution → Checkout Verification → GMV Attribution).
--
-- Layer 1 of the Agent Center; per-agent diagnoses tables (e.g.
-- agent_center_offer_execution_diagnoses) are added by individual
-- agent PRs as their payloads grow beyond what fits in
-- agent_center_issues.payload.
--
-- See docs/agent-center-v1.md for the architecture / state machine
-- / per-agent contract.

-- ---------------------------------------------------------------------------
-- 1. agent_center_merchant_stores — merchant ↔ store binding
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_center_merchant_stores (
    id              TEXT PRIMARY KEY,
    merchant_id     TEXT NOT NULL,
    store_id        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    deleted_at      TIMESTAMP WITH TIME ZONE NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_agent_center_merchant_stores_active
    ON agent_center_merchant_stores (merchant_id, store_id)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_agent_center_merchant_stores_merchant
    ON agent_center_merchant_stores (merchant_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_merchant_stores_status
    ON agent_center_merchant_stores (status);

-- ---------------------------------------------------------------------------
-- 2. agent_center_scan_targets — one job context per agent run
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_center_scan_targets (
    id              TEXT PRIMARY KEY,
    merchant_id     TEXT NOT NULL,
    store_id        TEXT NOT NULL,
    -- scan_mode covers all 5 agents' job kinds; the demand-test V1 modes
    -- are the four explicit *_test entries, the others are reserved for
    -- agents 2-5 so their migrations don't have to ALTER the CHECK constraint.
    scan_mode       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at      TIMESTAMP WITH TIME ZONE NULL,
    finished_at     TIMESTAMP WITH TIME ZONE NULL,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    deleted_at      TIMESTAMP WITH TIME ZONE NULL,
    CONSTRAINT chk_agent_center_scan_targets_status
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'stub_complete')),
    CONSTRAINT chk_agent_center_scan_targets_scan_mode
        CHECK (scan_mode IN (
            'open_product_visibility_test',
            'merchant_store_attribution_test',
            'pivota_pdp_attribution_test',
            'search_grounded_product_discovery_test',
            'sku_match',
            'offer_execution',
            'checkout_verification',
            'gmv_attribution'
        ))
);

CREATE INDEX IF NOT EXISTS idx_agent_center_scan_targets_merchant
    ON agent_center_scan_targets (merchant_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_scan_targets_store
    ON agent_center_scan_targets (store_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_scan_targets_status
    ON agent_center_scan_targets (status);
CREATE INDEX IF NOT EXISTS idx_agent_center_scan_targets_scan_mode
    ON agent_center_scan_targets (scan_mode);
CREATE INDEX IF NOT EXISTS idx_agent_center_scan_targets_created_at
    ON agent_center_scan_targets (created_at DESC);

-- ---------------------------------------------------------------------------
-- 3. agent_center_issues — shared blocker queue across all 5 agents
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_center_issues (
    id                  TEXT PRIMARY KEY,
    merchant_id         TEXT NOT NULL,
    store_id            TEXT NOT NULL,
    scan_target_id      TEXT NOT NULL,
    issue_type          TEXT NOT NULL,
    severity            TEXT NOT NULL DEFAULT 'medium',
    status              TEXT NOT NULL DEFAULT 'open',
    product_entity_id   TEXT NULL,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMP WITH TIME ZONE NULL,
    CONSTRAINT chk_agent_center_issues_status
        CHECK (status IN (
            'open', 'assigned', 'waiting_merchant_approval', 'in_progress',
            'ready_for_retest', 'retesting', 'resolved', 'rejected', 'ignored'
        )),
    CONSTRAINT chk_agent_center_issues_severity
        CHECK (severity IN ('low', 'medium', 'high', 'critical'))
);

CREATE INDEX IF NOT EXISTS idx_agent_center_issues_merchant
    ON agent_center_issues (merchant_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_issues_store
    ON agent_center_issues (store_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_issues_scan_target
    ON agent_center_issues (scan_target_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_issues_status
    ON agent_center_issues (status);
CREATE INDEX IF NOT EXISTS idx_agent_center_issues_issue_type
    ON agent_center_issues (issue_type);
CREATE INDEX IF NOT EXISTS idx_agent_center_issues_created_at
    ON agent_center_issues (created_at DESC);

-- ---------------------------------------------------------------------------
-- 4. agent_center_issue_resolution_plans — one plan per issue
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_center_issue_resolution_plans (
    id              TEXT PRIMARY KEY,
    issue_id        TEXT NOT NULL UNIQUE,
    merchant_id     TEXT NOT NULL,
    store_id        TEXT NOT NULL,
    scan_target_id  TEXT NOT NULL,
    blocker_type    TEXT NOT NULL,
    source_agent    TEXT NOT NULL DEFAULT 'resolution_workflow',
    status          TEXT NOT NULL DEFAULT 'draft',
    owner_type      TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    deleted_at      TIMESTAMP WITH TIME ZONE NULL,
    CONSTRAINT chk_agent_center_resolution_plans_owner_type
        CHECK (owner_type IN ('shared', 'pivota_ops', 'pivota_eng', 'human_review')),
    CONSTRAINT chk_agent_center_resolution_plans_status
        CHECK (status IN (
            'draft', 'assigned', 'in_progress',
            'waiting_merchant_approval', 'ready_for_retest',
            'resolved', 'rejected'
        ))
);

CREATE INDEX IF NOT EXISTS idx_agent_center_resolution_plans_issue
    ON agent_center_issue_resolution_plans (issue_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_resolution_plans_merchant
    ON agent_center_issue_resolution_plans (merchant_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_resolution_plans_status
    ON agent_center_issue_resolution_plans (status);
CREATE INDEX IF NOT EXISTS idx_agent_center_resolution_plans_owner_type
    ON agent_center_issue_resolution_plans (owner_type);

-- ---------------------------------------------------------------------------
-- 5. agent_center_usage_events — shared audit + idempotency meter
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_center_usage_events (
    id                  TEXT PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE,
    merchant_id         TEXT NOT NULL,
    store_id            TEXT NOT NULL,
    scan_target_id      TEXT NULL,
    issue_id            TEXT NULL,
    agent_type          TEXT NOT NULL,
    workflow_type       TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    provider            TEXT NOT NULL,
    scan_mode           TEXT NULL,
    billing_mode        TEXT NOT NULL DEFAULT 'preview_only',
    billing_status      TEXT NOT NULL DEFAULT 'not_invoiced',
    quantity            INTEGER NOT NULL DEFAULT 1,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_agent_center_usage_events_billing_mode
        CHECK (billing_mode IN ('preview_only', 'metered')),
    CONSTRAINT chk_agent_center_usage_events_billing_status
        CHECK (billing_status IN ('not_invoiced', 'invoiced', 'voided'))
);

CREATE INDEX IF NOT EXISTS idx_agent_center_usage_events_merchant
    ON agent_center_usage_events (merchant_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_usage_events_scan_target
    ON agent_center_usage_events (scan_target_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_usage_events_issue
    ON agent_center_usage_events (issue_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_usage_events_agent_type
    ON agent_center_usage_events (agent_type);
CREATE INDEX IF NOT EXISTS idx_agent_center_usage_events_created_at
    ON agent_center_usage_events (created_at DESC);

-- ---------------------------------------------------------------------------
-- 6. agent_center_production_validation_runs — smoke / pilot envelope
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_center_production_validation_runs (
    id                  TEXT PRIMARY KEY,
    status              TEXT NOT NULL DEFAULT 'queued',
    environment         TEXT NOT NULL,
    merchant_id         TEXT NULL,
    store_id            TEXT NULL,
    scan_target_id      TEXT NULL,
    product_entity_id   TEXT NULL,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMP WITH TIME ZONE NULL,
    deleted_at          TIMESTAMP WITH TIME ZONE NULL,
    CONSTRAINT chk_agent_center_validation_runs_status
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_agent_center_validation_runs_status
    ON agent_center_production_validation_runs (status);
CREATE INDEX IF NOT EXISTS idx_agent_center_validation_runs_environment
    ON agent_center_production_validation_runs (environment);
CREATE INDEX IF NOT EXISTS idx_agent_center_validation_runs_merchant
    ON agent_center_production_validation_runs (merchant_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_validation_runs_created_at
    ON agent_center_production_validation_runs (created_at DESC);

-- ---------------------------------------------------------------------------
-- 7. agent_center_demo_fixtures — internal-only test seed (gated)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_center_demo_fixtures (
    id              TEXT PRIMARY KEY,
    fixture_id      TEXT NOT NULL UNIQUE,
    preset          TEXT NOT NULL,
    environment     TEXT NOT NULL,
    cleanup_status  TEXT NOT NULL DEFAULT 'active',
    status          TEXT NOT NULL DEFAULT 'created',
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    ttl_minutes     INTEGER NULL,
    expires_at      TIMESTAMP WITH TIME ZONE NULL,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_agent_center_demo_fixtures_cleanup_status
        CHECK (cleanup_status IN ('active', 'expired', 'deleted'))
);

CREATE INDEX IF NOT EXISTS idx_agent_center_demo_fixtures_fixture_id
    ON agent_center_demo_fixtures (fixture_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_demo_fixtures_preset
    ON agent_center_demo_fixtures (preset);
CREATE INDEX IF NOT EXISTS idx_agent_center_demo_fixtures_cleanup_status
    ON agent_center_demo_fixtures (cleanup_status);
CREATE INDEX IF NOT EXISTS idx_agent_center_demo_fixtures_expires_at
    ON agent_center_demo_fixtures (expires_at);

-- ---------------------------------------------------------------------------
-- updated_at triggers (mirror the webhook_events migration pattern)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_agent_center_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_agent_center_merchant_stores_updated_at
    ON agent_center_merchant_stores;
CREATE TRIGGER trg_agent_center_merchant_stores_updated_at
    BEFORE UPDATE ON agent_center_merchant_stores
    FOR EACH ROW EXECUTE FUNCTION update_agent_center_updated_at();

DROP TRIGGER IF EXISTS trg_agent_center_scan_targets_updated_at
    ON agent_center_scan_targets;
CREATE TRIGGER trg_agent_center_scan_targets_updated_at
    BEFORE UPDATE ON agent_center_scan_targets
    FOR EACH ROW EXECUTE FUNCTION update_agent_center_updated_at();

DROP TRIGGER IF EXISTS trg_agent_center_issues_updated_at
    ON agent_center_issues;
CREATE TRIGGER trg_agent_center_issues_updated_at
    BEFORE UPDATE ON agent_center_issues
    FOR EACH ROW EXECUTE FUNCTION update_agent_center_updated_at();

DROP TRIGGER IF EXISTS trg_agent_center_resolution_plans_updated_at
    ON agent_center_issue_resolution_plans;
CREATE TRIGGER trg_agent_center_resolution_plans_updated_at
    BEFORE UPDATE ON agent_center_issue_resolution_plans
    FOR EACH ROW EXECUTE FUNCTION update_agent_center_updated_at();

DROP TRIGGER IF EXISTS trg_agent_center_validation_runs_updated_at
    ON agent_center_production_validation_runs;
CREATE TRIGGER trg_agent_center_validation_runs_updated_at
    BEFORE UPDATE ON agent_center_production_validation_runs
    FOR EACH ROW EXECUTE FUNCTION update_agent_center_updated_at();

DROP TRIGGER IF EXISTS trg_agent_center_demo_fixtures_updated_at
    ON agent_center_demo_fixtures;
CREATE TRIGGER trg_agent_center_demo_fixtures_updated_at
    BEFORE UPDATE ON agent_center_demo_fixtures
    FOR EACH ROW EXECUTE FUNCTION update_agent_center_updated_at();

-- ---------------------------------------------------------------------------
-- Comments
-- ---------------------------------------------------------------------------
COMMENT ON TABLE agent_center_merchant_stores IS
    'Agent Center: merchant ↔ store binding (one row per active merchant store)';
COMMENT ON TABLE agent_center_scan_targets IS
    'Agent Center: a single job context per agent run (which merchant, which mode)';
COMMENT ON TABLE agent_center_issues IS
    'Agent Center: shared blocker queue across all 5 agents';
COMMENT ON TABLE agent_center_issue_resolution_plans IS
    'Agent Center: deterministic remediation plan for each issue (one per issue)';
COMMENT ON TABLE agent_center_usage_events IS
    'Agent Center: shared audit / usage meter; UNIQUE(idempotency_key) for replay safety';
COMMENT ON TABLE agent_center_production_validation_runs IS
    'Agent Center: smoke test / pilot validation envelopes (internal-only)';
COMMENT ON TABLE agent_center_demo_fixtures IS
    'Agent Center: internal-only seed data for smoke testing (gated by ENABLE_INTERNAL_DEMO_FIXTURES)';

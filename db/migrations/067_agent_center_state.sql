-- 067_agent_center_state.sql
-- Durable Agent Center state tables for merchant pilot readiness.
-- The merchant portal runtime should use a restricted role scoped to this schema.

CREATE SCHEMA IF NOT EXISTS agent_center;

CREATE TABLE IF NOT EXISTS agent_center.agent_center_merchant_stores (
  id TEXT PRIMARY KEY,
  merchant_id TEXT NULL,
  store_id TEXT NULL,
  scan_target_id TEXT NULL,
  issue_id TEXT NULL,
  fixture_id TEXT NULL,
  production_validation_run_id TEXT NULL,
  product_entity_id TEXT NULL,
  agent_type TEXT NULL,
  provider TEXT NULL,
  status TEXT NULL,
  idempotency_key TEXT NULL,
  workflow_type TEXT NULL,
  event_type TEXT NULL,
  billing_mode TEXT NULL,
  billing_status TEXT NULL,
  quantity NUMERIC NULL,
  environment TEXT NULL,
  preset TEXT NULL,
  cleanup_status TEXT NULL,
  readiness_level TEXT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  deleted_at TIMESTAMPTZ NULL,
  completed_at TIMESTAMPTZ NULL,
  expires_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS agent_center.agent_center_scan_targets (LIKE agent_center.agent_center_merchant_stores INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);
CREATE TABLE IF NOT EXISTS agent_center.agent_center_issues (LIKE agent_center.agent_center_merchant_stores INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);
CREATE TABLE IF NOT EXISTS agent_center.agent_center_product_understanding_diagnoses (LIKE agent_center.agent_center_merchant_stores INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);
CREATE TABLE IF NOT EXISTS agent_center.agent_center_offer_execution_diagnoses (LIKE agent_center.agent_center_merchant_stores INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);
CREATE TABLE IF NOT EXISTS agent_center.agent_center_checkout_verification_diagnoses (LIKE agent_center.agent_center_merchant_stores INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);
CREATE TABLE IF NOT EXISTS agent_center.agent_center_gmv_assurance_snapshots (LIKE agent_center.agent_center_merchant_stores INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);
CREATE TABLE IF NOT EXISTS agent_center.agent_center_issue_resolution_plans (LIKE agent_center.agent_center_merchant_stores INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);
CREATE TABLE IF NOT EXISTS agent_center.agent_center_usage_events (LIKE agent_center.agent_center_merchant_stores INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);
CREATE TABLE IF NOT EXISTS agent_center.agent_center_production_validation_runs (LIKE agent_center.agent_center_merchant_stores INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);
CREATE TABLE IF NOT EXISTS agent_center.agent_center_demo_fixtures (LIKE agent_center.agent_center_merchant_stores INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_center_usage_events_idempotency_key
  ON agent_center.agent_center_usage_events (idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_center_merchant_stores_merchant_created
  ON agent_center.agent_center_merchant_stores (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_scan_targets_merchant_created
  ON agent_center.agent_center_scan_targets (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_scan_targets_store_created
  ON agent_center.agent_center_scan_targets (store_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_issues_merchant_created
  ON agent_center.agent_center_issues (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_issues_store_created
  ON agent_center.agent_center_issues (store_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_issues_scan_target_created
  ON agent_center.agent_center_issues (scan_target_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_issues_status
  ON agent_center.agent_center_issues (status);
CREATE INDEX IF NOT EXISTS idx_agent_center_issues_product_entity
  ON agent_center.agent_center_issues (product_entity_id);

CREATE INDEX IF NOT EXISTS idx_agent_center_pu_issue_created
  ON agent_center.agent_center_product_understanding_diagnoses (issue_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_offer_issue_created
  ON agent_center.agent_center_offer_execution_diagnoses (issue_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_checkout_issue_created
  ON agent_center.agent_center_checkout_verification_diagnoses (issue_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_center_snapshots_merchant_created
  ON agent_center.agent_center_gmv_assurance_snapshots (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_snapshots_store_created
  ON agent_center.agent_center_gmv_assurance_snapshots (store_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_snapshots_product_created
  ON agent_center.agent_center_gmv_assurance_snapshots (product_entity_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_center_resolution_issue_created
  ON agent_center.agent_center_issue_resolution_plans (issue_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_resolution_status
  ON agent_center.agent_center_issue_resolution_plans (status);

CREATE INDEX IF NOT EXISTS idx_agent_center_usage_merchant_created
  ON agent_center.agent_center_usage_events (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_usage_store_created
  ON agent_center.agent_center_usage_events (store_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_usage_agent_provider
  ON agent_center.agent_center_usage_events (agent_type, provider);
CREATE INDEX IF NOT EXISTS idx_agent_center_usage_billing
  ON agent_center.agent_center_usage_events (billing_mode, billing_status);

CREATE INDEX IF NOT EXISTS idx_agent_center_validation_status_created
  ON agent_center.agent_center_production_validation_runs (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_center_validation_run_lookup
  ON agent_center.agent_center_production_validation_runs (production_validation_run_id);

CREATE INDEX IF NOT EXISTS idx_agent_center_fixtures_fixture_id
  ON agent_center.agent_center_demo_fixtures (fixture_id);
CREATE INDEX IF NOT EXISTS idx_agent_center_fixtures_cleanup_expires
  ON agent_center.agent_center_demo_fixtures (cleanup_status, expires_at);

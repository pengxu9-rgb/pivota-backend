-- 265: DURABLE PURCHASE EVENTS FOR AGENT PLATFORMS (payment orchestration P1).
--
-- One row per (purchase, rail state) that warrants telling the agent: purchase.action_required,
-- purchase.completed, purchase.failed. services/payment_orchestration/purchase_events.py detects and
-- WRITES them here before anything is sent, then hands each to the agent webhook sender with its
-- stored event_id, so every delivery and retry of one event carries the same id (receivers
-- de-duplicate on it). The UNIQUE (purchase_id, rail_state) index makes detection idempotent; the
-- claim columns let exactly one worker hand an event over at a time.
--
-- No PII: payload is the owner-scoped public purchase view. The same DDL is in
-- db/agent_purchase_events._CREATE_TABLE_PG, run by db/schema_guard.ensure_required_schema_light;
-- tests/test_agent_purchase_events_postgres.py proves the two build the same schema. A new, empty
-- table: safe under the boot runner's single transaction with no lock_timeout.

CREATE TABLE IF NOT EXISTS agent_purchase_events (
    event_id VARCHAR(64) PRIMARY KEY,
    purchase_id VARCHAR(64) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    rail VARCHAR(16) NOT NULL,
    rail_state VARCHAR(32) NOT NULL,
    unified_state VARCHAR(40) NOT NULL,
    event_type VARCHAR(40) NOT NULL CONSTRAINT ck_agent_purchase_events_type
        CHECK (event_type IN ('purchase.action_required', 'purchase.completed', 'purchase.failed')),
    payload JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending' CONSTRAINT ck_agent_purchase_events_status
        CHECK (status IN ('pending', 'handed_over', 'skipped', 'abandoned')),
    attempts INTEGER NOT NULL DEFAULT 0,
    claimed_by VARCHAR(64),
    claimed_at TIMESTAMPTZ,
    delivery_id VARCHAR(64),
    delivery_status VARCHAR(32),
    skip_reason VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    handed_over_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_purchase_events_purchase_state
    ON agent_purchase_events (purchase_id, rail_state);

CREATE INDEX IF NOT EXISTS idx_agent_purchase_events_status_created
    ON agent_purchase_events (status, created_at);

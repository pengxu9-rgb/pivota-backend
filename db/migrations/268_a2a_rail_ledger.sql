-- 268: THE A2A TOKEN RAIL'S LEDGER (payment orchestration P2): buyer refs + purchases, and the
-- parent ledger's rail vocabulary widened to admit it.
--
-- a2a_rail_buyer_refs: one opaque member id per (agent, end user), minted, never derived.
-- a2a_rail_purchases:  one row per purchase attempt, a state machine in a column (executor
--                      rail_managed: the rail's checkout agent places the merchant order).
--
-- The same DDL is in db/a2a_rail_ledger (`ensure_a2a_rail_schema`), run by
-- db/schema_guard.ensure_required_schema_light on both dialects; tests/test_a2a_rail_ledger_postgres.py
-- proves the two build the same schema. Two new, empty tables lock nothing that exists.
--
-- THE PARENT'S CHECK. agent_purchases.ck_agent_purchases_rail is dropped and re-added with 'a2a'
-- (named in 264 for exactly this). It locks agent_purchases briefly (ACCESS EXCLUSIVE, a validating
-- scan of a small table); the lock_timeout below bounds the wait instead of queueing writers behind
-- it. The boot heal (db/agent_purchase_ledger.ensure_agent_purchase_schema) does the same inside
-- schema_guard.guarded_ddl, only while the constraint differs, under the same bound.
--
-- schema-guard-exempt: this migration adds no column to an existing table.

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS a2a_rail_buyer_refs (
    member_id VARCHAR(40) PRIMARY KEY,
    agent_id VARCHAR(128) NOT NULL,
    agent_user_ref_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_a2a_rail_buyer_refs_owner
    ON a2a_rail_buyer_refs (agent_id, agent_user_ref_hash);

CREATE TABLE IF NOT EXISTS a2a_rail_purchases (
    id VARCHAR(40) PRIMARY KEY,
    member_id VARCHAR(40) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    agent_user_ref_hash VARCHAR(64) NOT NULL,
    state VARCHAR(32) NOT NULL DEFAULT 'opened' CONSTRAINT ck_a2a_rail_purchases_state CHECK (state IN (
        'opened', 'needs_payment_method', 'quoting', 'awaiting_authorization', 'paying',
        'completed', 'failed', 'refused', 'expired')),
    merchant_domain VARCHAR(253) NOT NULL,
    checkout_url TEXT NOT NULL,
    product_key VARCHAR(255),
    product_name TEXT,
    variant_title TEXT,
    quantity INTEGER NOT NULL DEFAULT 1 CONSTRAINT ck_a2a_rail_purchases_quantity CHECK (quantity > 0),
    currency VARCHAR(3) NOT NULL,
    our_price_minor BIGINT NOT NULL CONSTRAINT ck_a2a_rail_purchases_price CHECK (our_price_minor > 0),
    consent_version VARCHAR(32),
    consented_at TIMESTAMPTZ,
    payment_method_id VARCHAR(128),
    payment_token_id VARCHAR(128),
    quote_id VARCHAR(128),
    order_id VARCHAR(128),
    quoted_total_minor BIGINT,
    final_total_minor BIGINT,
    shipping_minor BIGINT,
    tax_minor BIGINT,
    buyer_action_kind VARCHAR(24) CONSTRAINT ck_a2a_rail_purchases_action CHECK (
        buyer_action_kind IS NULL OR buyer_action_kind IN ('card_binding', 'approval', 'authenticate')),
    buyer_action_url TEXT,
    buyer_action_expires_at TIMESTAMPTZ,
    merchant_order_id VARCHAR(128),
    merchant_order_status_url TEXT,
    refusal_reason VARCHAR(64),
    last_error_code VARCHAR(64),
    pending_write VARCHAR(64),
    pending_write_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_poll_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_by VARCHAR(64),
    claimed_at TIMESTAMPTZ,
    buyer_email TEXT,
    shipping_address JSONB,
    contact_purged_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    state_entered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    terminal_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_a2a_rail_purchases_owner
    ON a2a_rail_purchases (agent_id, agent_user_ref_hash, created_at);

CREATE INDEX IF NOT EXISTS idx_a2a_rail_purchases_due
    ON a2a_rail_purchases (state, next_poll_at);

ALTER TABLE agent_purchases DROP CONSTRAINT IF EXISTS ck_agent_purchases_rail;
ALTER TABLE agent_purchases ADD CONSTRAINT ck_agent_purchases_rail CHECK (rail IN ('reap', 'a2a'));

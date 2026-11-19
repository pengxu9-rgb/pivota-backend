-- 291: THE AFFILIATE PROGRAM REGISTRY (developer earnings, manual stage).
--
-- One row per merchant affiliate program Pivota has applied to, or plans to apply to, on a network
-- (Rakuten, Awin, Impact, ...). Staff manage it in the employee portal through
-- routes/employee_affiliate_programs.py (dark: AFFILIATE_PROGRAM_REGISTRY_ENABLED, default off).
-- It answers three questions per program: where the application stands, whether AI-agent traffic may
-- earn on it (software publishers allowed AND revenue sharing with sub-publishers allowed), and
-- whether our outbound links already carry Pivota's publisher id.
--
-- LIVE IS DERIVED AND GUARDED: `live_at` is set only by the mark-live route, and the CHECK below refuses
-- a live row that is not approved with both terms allowed and links live, on both dialects. Nothing in
-- this table moves money, issues links or touches clicks; it is a registry.
--
-- `affiliate_program_events` is APPEND-ONLY: every create, update, import, mark-live and un-live writes
-- one row; nothing updates or deletes it (a test greps the repo for that).
--
-- The same DDL is in db/affiliate_programs._CREATE_*_PG, run by db/schema_guard.ensure_required_schema_light
-- on both dialects (a fast-mode boot skips db/migrations/); tests/test_affiliate_programs_postgres.py proves
-- the two build the same schema. New, empty tables and two indexes on them: safe under the boot runner's
-- single transaction with no lock_timeout.

CREATE TABLE IF NOT EXISTS affiliate_programs (
    id VARCHAR(40) PRIMARY KEY,
    network VARCHAR(32) NOT NULL CONSTRAINT ck_affiliate_programs_network CHECK (network IN ('rakuten', 'awin', 'impact', 'cj', 'partnerize', 'shareasale', 'skimlinks', 'involve_asia', 'flexoffers', 'other')),
    advertiser_id VARCHAR(64),
    merchant_name VARCHAR(200) NOT NULL,
    merchant_domains TEXT NOT NULL DEFAULT '[]',
    status VARCHAR(16) NOT NULL DEFAULT 'not_applied' CONSTRAINT ck_affiliate_programs_status CHECK (status IN ('not_applied', 'applied', 'approved', 'rejected', 'paused')),
    software_publishers_allowed VARCHAR(16) NOT NULL DEFAULT 'unknown' CONSTRAINT ck_affiliate_programs_software CHECK (software_publishers_allowed IN ('unknown', 'allowed', 'not_allowed')),
    revenue_share_allowed VARCHAR(16) NOT NULL DEFAULT 'unknown' CONSTRAINT ck_affiliate_programs_revshare CHECK (revenue_share_allowed IN ('unknown', 'allowed', 'not_allowed')),
    publisher_id_links_live BOOLEAN NOT NULL DEFAULT FALSE,
    commission_rate_bp INTEGER CONSTRAINT ck_affiliate_programs_rate CHECK (commission_rate_bp IS NULL OR (commission_rate_bp >= 0 AND commission_rate_bp <= 10000)),
    rate_note VARCHAR(500),
    return_window_days INTEGER CONSTRAINT ck_affiliate_programs_return CHECK (return_window_days IS NULL OR return_window_days >= 0),
    applied_at TIMESTAMPTZ,
    decided_at TIMESTAMPTZ,
    live_at TIMESTAMPTZ,
    notes TEXT,
    created_by VARCHAR(200),
    updated_by VARCHAR(200),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_affiliate_programs_live CHECK (live_at IS NULL OR (status = 'approved' AND software_publishers_allowed = 'allowed' AND revenue_share_allowed = 'allowed' AND publisher_id_links_live))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_affiliate_programs_network_advertiser
    ON affiliate_programs (network, advertiser_id) WHERE advertiser_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS affiliate_program_events (
    id VARCHAR(40) PRIMARY KEY,
    program_id VARCHAR(40) NOT NULL REFERENCES affiliate_programs (id),
    actor VARCHAR(200) NOT NULL,
    action VARCHAR(32) NOT NULL CONSTRAINT ck_affiliate_program_events_action CHECK (action IN ('create', 'update', 'import_create', 'import_update', 'mark_live', 'unlive', 'auto_unlive')),
    before_state TEXT,
    after_state TEXT,
    reason VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_affiliate_program_events_program
    ON affiliate_program_events (program_id, created_at);

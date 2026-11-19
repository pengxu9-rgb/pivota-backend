-- 289: THE BUYER'S ACCEPTANCE OF PIVOTA'S TERMS AND PRIVACY POLICY ON AN AGENPAS PAGE.
--
-- A payment partner's production rule: the buyer sees and accepts Pivota's terms of service AND privacy
-- policy before a card is stored and again before every approval, and the acceptance is recorded in OUR
-- system. One row per (rail purchase, step, approval epoch): `card_binding` before the rail's card page,
-- `approval` before its approval page. Written only by POST /agent/internal/commerce/purchases/{id}/consents
-- (dark: AGENPAS_CONSENT_WRITE_ENABLED, default off); read by the hosted-URL gate (AGENPAS_CONSENT_GATE,
-- default off) in the rail view builders.
--
-- INSERT-ONLY: nothing updates or deletes a row, and no PII scrub or purchase cleanup touches the table.
-- No PII in it: no IP, no user agent, no email. `evidence` says what the row proves and no more: the holder
-- of the AgenPas link accepted on Pivota's page (the agent holds the link too).
--
-- The same DDL is in db/agent_purchase_consents._CREATE_TABLE_PG, run by
-- db/schema_guard.ensure_required_schema_light on both dialects (a fast-mode boot skips db/migrations/);
-- tests/test_agenpas_consent_gate_postgres.py proves the two build the same schema. A new, empty table and
-- one index on it: safe under the boot runner's single transaction with no lock_timeout.

CREATE TABLE IF NOT EXISTS agent_purchase_consents (
    id VARCHAR(40) PRIMARY KEY,
    purchase_id VARCHAR(64),
    rail VARCHAR(16) NOT NULL CONSTRAINT ck_agent_purchase_consents_rail CHECK (rail IN ('reap', 'a2a')),
    rail_purchase_id VARCHAR(64) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    step VARCHAR(16) NOT NULL CONSTRAINT ck_agent_purchase_consents_step CHECK (step IN ('card_binding', 'approval')),
    approval_epoch INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_agent_purchase_consents_epoch CHECK (approval_epoch >= 0),
    terms_version VARCHAR(32) NOT NULL,
    privacy_version VARCHAR(32) NOT NULL,
    terms_url TEXT NOT NULL,
    privacy_url TEXT NOT NULL,
    evidence VARCHAR(40) NOT NULL CONSTRAINT ck_agent_purchase_consents_evidence CHECK (evidence IN ('link_holder_on_pivota_page')),
    rail_buyer_ref VARCHAR(128),
    offer_issued_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_purchase_consents_step
    ON agent_purchase_consents (rail_purchase_id, rail, step, approval_epoch);

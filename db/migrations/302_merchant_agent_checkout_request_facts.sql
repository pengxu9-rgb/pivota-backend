-- 302: WHAT A SELLER'S OWN UCP DOOR SAID WHEN THE GATEWAY BUILT A CART THERE (Tier 2, request-time).
--
-- When the gateway builds a cart at a seller's own UCP door it hands the backend the raw evidence it
-- read (the `/.well-known/ucp` status and body). The backend RE-PARSES that body with the census
-- job's own parser and classifier (jobs/merchant_protocol_profile_census.build_row / checkout_check)
-- and keeps the result here. The caller never sends a verdict.
--
-- A SEPARATE TABLE, ON PURPOSE. The run condition (services/payment_orchestration/agent_run_condition
-- through db/merchant_agent_checkout_facts.run_condition_answer) reads merchant_agent_checkout_facts
-- (migration 294) only, so it keeps counting CENSUS positives only. Whether a request-time positive
-- ever counts is an owner decision that has not been made; until it is, nothing reads this table
-- for the run condition, and the census population (`--from-directory`) does not read it either.
--
-- THE SAME COLUMNS AND TYPES AS 294, so a later reader that ORs the two tables is one UNION. No
-- foreign key to the census table: a request fact may exist for a domain the census never stored.
-- `source` is always 'request'.
--
-- THE WRITE RULES (db/merchant_agent_checkout_request_facts.record_request_check, one conditional
-- upsert whose every time comparison is the database's own clock):
--   * an unreadable check writes nothing;
--   * at most one write per domain per 24 hours;
--   * a positive renews a positive, and replaces a negative only once that negative is 14 days old;
--   * a negative never overwrites a positive younger than the fact max age (30 days by default).
--
-- NO BUYER DATA AND NO DOCUMENT BODY. `last_code` is a short token from the census reader's own
-- constants; `ucp_version` is a date. Nothing the merchant's server sent is stored verbatim.
--
-- THIS TABLE HAS NO STARTUP HEAL, ON PURPOSE (as 294). Nothing in db/schema_guard creates it.
-- PRODUCTION boots skip db/migrations/ (main._should_skip_heavy_startup_init answers is_production()
-- unless SKIP_HEAVY_STARTUP_INIT says otherwise), so a production deploy never builds it; there this
-- file is applied once, by hand, as a gated one-off after its PR has merged. A NON-PRODUCTION boot
-- (staging, a local run) runs db/migrations/ through db.sql_migrations.run_sql_migrations and DOES
-- build it at boot. The writer treats a missing table as "nothing written" and warns about it at
-- most once per interval.

CREATE TABLE IF NOT EXISTS merchant_agent_checkout_request_facts (
    merchant_domain VARCHAR(255) PRIMARY KEY,
    checked_at TIMESTAMPTZ NOT NULL,
    last_outcome VARCHAR(20) NOT NULL
        CONSTRAINT ck_merchant_agent_checkout_request_facts_outcome
        CHECK (last_outcome IN ('publishes', 'does_not_publish', 'unreadable')),
    last_code VARCHAR(64) NOT NULL,
    ucp_checkout_at TIMESTAMPTZ,
    acp_checkout_at TIMESTAMPTZ,
    negative_at TIMESTAMPTZ,
    ucp_version VARCHAR(10),
    source VARCHAR(32) NOT NULL
);

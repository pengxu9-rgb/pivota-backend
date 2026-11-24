-- 294: THE STORED FACT "THIS MERCHANT PUBLISHES AN AGENTIC CHECKOUT PROTOCOL" (ADR-030 D8, D11).
--
-- ADR-030 D11 lets a payment partner's checkout agent run only on a merchant that publishes an
-- agentic checkout protocol, and D8 says that is read from a stored fact, never from a request-time
-- fetch. Until this table, the only reader of a merchant's /.well-known documents
-- (jobs/merchant_protocol_profile_census) printed what it found and stored none of it.
--
-- ONE ROW PER MERCHANT DOMAIN (the key is db.merchant_purchasability.normalize_domain's form:
-- lower case, one leading `www.` removed). The fact is about the domain, not a market.
--
-- EACH PATH IS READ ON ITS OWN. A readable document that offers checkout sets that path's
-- `*_checkout_at`; a path that answers cleanly without it (a readable document that does not list
-- checkout, or a 404 / 410) clears that path's `*_checkout_at`; a path that could not be read
-- (robots.txt, a refused redirect, a timeout, a 403 or 5xx, a body that does not parse) leaves its
-- column as it was. "Cannot tell" is never turned into a negative and never renews a positive.
--
-- ONE OUTCOME FOR THE WHOLE CHECK:
--   publishes          at least one path offers checkout. Clears `negative_at`.
--   does_not_publish   both paths answered cleanly and neither offers checkout. Sets
--                      `negative_at`.
--   unreadable         neither of the above. `negative_at` is left as it was.
--
-- THE READ (db/merchant_agent_checkout_facts.meets_run_condition) is true only while
-- `negative_at` is NULL and one of the two `*_checkout_at` is inside the freshness window. A
-- missing row, an old positive and a negative all answer false.
--
-- NO BUYER DATA AND NO DOCUMENT BODY. `last_code` is a short token from the reader's own
-- constants; `ucp_version` is a date. Nothing a merchant's server sent is stored verbatim.
--
-- THIS TABLE HAS NO STARTUP HEAL, ON PURPOSE. Production does not run db/migrations/ at boot, and
-- this file is applied there once, by hand, as a gated one-off after its PR has merged. Nothing
-- in db/schema_guard creates it, so a deploy never builds it ahead of that run. Every reader and
-- writer treats a missing table as "no fact": the read answers false, and the census job refuses
-- to start a storing run before its first request.

CREATE TABLE IF NOT EXISTS merchant_agent_checkout_facts (
    merchant_domain VARCHAR(255) PRIMARY KEY,
    checked_at TIMESTAMPTZ NOT NULL,
    last_outcome VARCHAR(20) NOT NULL
        CONSTRAINT ck_merchant_agent_checkout_facts_outcome
        CHECK (last_outcome IN ('publishes', 'does_not_publish', 'unreadable')),
    last_code VARCHAR(64) NOT NULL,
    ucp_checkout_at TIMESTAMPTZ,
    acp_checkout_at TIMESTAMPTZ,
    negative_at TIMESTAMPTZ,
    ucp_version VARCHAR(10),
    source VARCHAR(32) NOT NULL
);

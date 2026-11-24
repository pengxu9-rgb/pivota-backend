-- 295: THE RECORD OF A MERCHANT CHALLENGING OR BLOCKING A PAYMENT PARTNER'S CHECKOUT AGENT
-- (ADR-030 D11).
--
-- D11: after a CAPTCHA, a bot challenge or a block, that merchant is not completable by that
-- partner's agent, and it is re-enabled only on evidence: the merchant verifies the agent under a
-- card network's agent programme, or gives written permission. Publishing the protocol does not
-- re-enable it, and neither does a later order. Until this table nothing recorded such an event:
-- a CAPTCHA only failed the one purchase that met it.
--
-- ONE ROW PER EVENT, KEYED TO THE PARTNER'S RAIL AND THE MERCHANT DOMAIN. The key is the domain,
-- not a market: the merchant challenged the agent, wherever the buyer was. `merchant_domain` is in
-- db.merchant_purchasability.normalize_domain's form.
--
-- THE EVENT IS NEVER REWRITTEN AND NEVER DELETED. The only change a row ever sees is its four
-- `cleared_*` columns going from NULL to set, once, together (the CHECK below): who cleared it, on
-- which of the two bases D11 allows, and where the evidence is kept. A row that is cleared stays
-- in the table, so the history of a merchant is every row it ever had.
--
-- THE READ (db/partner_agent_merchant_blocks.is_clear) is true only when the read worked and no
-- row for that rail and domain has `cleared_at` NULL. A failed read is not "clear".
--
-- `purchase_id` is OUR purchase id, when a purchase met the event. One purchase records one event:
-- the unique index makes a worker's retry a no-op. An operator-entered row has none.
--
-- NO BUYER DATA. `recorded_by` / `cleared_by` name a member of staff, `cleared_evidence` says
-- where the evidence is kept (a ticket or a document reference), never the evidence itself.
--
-- THIS TABLE HAS NO STARTUP HEAL, ON PURPOSE (as migration 294): it is applied in production once,
-- by hand, as a gated one-off after its PR has merged. Nothing in db/schema_guard creates it, and
-- every reader and writer treats a missing table as "cannot tell".

CREATE TABLE IF NOT EXISTS partner_agent_merchant_blocks (
    id VARCHAR(40) PRIMARY KEY,
    rail VARCHAR(16) NOT NULL
        CONSTRAINT ck_partner_agent_merchant_blocks_rail CHECK (rail IN ('reap', 'a2a')),
    merchant_domain VARCHAR(255) NOT NULL,
    kind VARCHAR(16) NOT NULL
        CONSTRAINT ck_partner_agent_merchant_blocks_kind
        CHECK (kind IN ('captcha', 'bot_challenge', 'block')),
    source VARCHAR(16) NOT NULL
        CONSTRAINT ck_partner_agent_merchant_blocks_source CHECK (source IN ('worker', 'operator')),
    purchase_id VARCHAR(64),
    recorded_by VARCHAR(200),
    observed_at TIMESTAMPTZ NOT NULL,
    cleared_at TIMESTAMPTZ,
    cleared_basis VARCHAR(24)
        CONSTRAINT ck_partner_agent_merchant_blocks_basis
        CHECK (cleared_basis IS NULL OR cleared_basis IN ('verified_recognition', 'written_permission')),
    cleared_by VARCHAR(200),
    cleared_evidence VARCHAR(500),
    CONSTRAINT ck_partner_agent_merchant_blocks_cleared_together CHECK (
        (cleared_at IS NULL AND cleared_basis IS NULL AND cleared_by IS NULL AND cleared_evidence IS NULL)
        OR (cleared_at IS NOT NULL AND cleared_basis IS NOT NULL AND cleared_by IS NOT NULL
            AND cleared_evidence IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_partner_agent_merchant_blocks_purchase
    ON partner_agent_merchant_blocks (purchase_id) WHERE purchase_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_partner_agent_merchant_blocks_open
    ON partner_agent_merchant_blocks (rail, merchant_domain) WHERE cleared_at IS NULL;

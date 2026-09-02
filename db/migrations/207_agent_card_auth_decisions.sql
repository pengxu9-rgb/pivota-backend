-- AGENT CARD AUTHORIZATION DECISIONS — the LIVE half of the Reap card rail.
--
-- Migration 201 mints the instrument, 202 records what the issuer reported afterwards. This
-- migration adds the third moment, which sits BETWEEN them: Reap's external-authorization
-- request. With the sandbox project configured Program-Funded + External authorization, every
-- card authorization pauses at the network while Reap POSTs us a CARD_AUTHORIZATION_REQUEST
-- and waits up to 1.6 seconds for {"decision":"APPROVE"} or {"decision":"DECLINE",...}.
--
-- WHY A LEDGER AND NOT A COUNTER ON THE CARD ROW. The decision is not the record: the
-- authoritative record is the CARD_TRANSACTION_CREATED webhook that arrives afterwards and
-- carries our eventId as triggerEventId. The webhook path (apply_auth_approved) is guarded on
-- status='issued' and alarms AUTH_ON_NON_ISSUED_CARD when it is not — so if the live decision
-- moved the card's status, every approval we granted would make its own record alarm falsely.
-- The decision therefore touches NOTHING on agent_issued_cards. It writes here instead, and
-- this table is what makes single-use atomic across the gap between decision and record:
-- rule (d) reserves the card by looking for a prior APPROVE row, under the same per-card
-- advisory lock the decision runs in, so two concurrent authorizations serialize and exactly
-- one of them can approve.
--
-- WHY event_id IS THE PRIMARY KEY. Idempotency is the whole contract for a synchronous
-- decision endpoint: a retried authorization request must be answered with the SAME verdict we
-- already gave, not re-evaluated against state our own earlier answer changed. The PK makes
-- the replay a lookup, and makes "one row per decision" a constraint rather than a convention.
--
-- reason (Reap's vocabulary, only two values exist) and reason_code (OURS) are separate
-- columns on purpose: Reap's INSUFFICIENT_BALANCE / TRANSACTION_NOT_ALLOWED is what the
-- cardholder's terminal shows, and it is far too coarse to operate on. reason_code is the rule
-- that actually fired, and it is the column ops reads.
CREATE TABLE IF NOT EXISTS agent_card_auth_decisions (
    -- data.eventId from the request. Reap's later CARD_TRANSACTION_CREATED webhook echoes this
    -- as triggerEventId, which is the join from decision to record.
    event_id VARCHAR(128) PRIMARY KEY,

    -- OUR card id. NULLABLE, and the null case is load-bearing: an authorization for a card
    -- ref we never minted (rule b) still gets a decision row, because "we declined something
    -- we cannot explain" is exactly the event that must not be invisible.
    card_id VARCHAR(64),

    -- Reap's card id, always present — it is how the request identified itself.
    issuer_card_ref VARCHAR(128) NOT NULL,

    decision VARCHAR(8) NOT NULL CHECK (decision IN ('APPROVE', 'DECLINE')),

    -- Reap's decline vocabulary: INSUFFICIENT_BALANCE | TRANSACTION_NOT_ALLOWED. NULL on
    -- APPROVE (an approval carries no reason on the wire).
    reason VARCHAR(32),

    -- Ours. The rule that fired: approved | unknown_card | card_not_live | card_expired |
    -- already_authorized | channel_not_allowed | currency_mismatch | amount_unparseable |
    -- over_cap | merchant_mismatch. NOT NULL — every path names its rule.
    reason_code VARCHAR(48) NOT NULL,

    -- The amount IN THE CARD'S CURRENCY, minor units, as the cap comparison saw it. NULL when
    -- the decision fired before an amount could be resolved (unknown card, wrong channel, a
    -- currency we could not match, an amount we refused to round).
    amount_minor BIGINT,
    currency VARCHAR(8),

    channel VARCHAR(16),

    -- The merchant descriptor as it appeared on the authorization. Kept RAW here (this is the
    -- evidence a mismatch investigation reads); the normalized form lives in the descriptor
    -- registry below.
    merchant_name TEXT,
    merchant_city TEXT,
    merchant_country VARCHAR(2),
    mcc VARCHAR(4),

    -- FALSE means the descriptor was not pinned for this merchant_domain at decision time. It
    -- is not a decline: see the registry note below.
    merchant_verified BOOLEAN NOT NULL DEFAULT FALSE,

    -- Wall clock from request receipt to decision. NOT NULL because the budget is 1.6s and a
    -- decision we cannot time is a decision we cannot defend; the handler warns above 800ms.
    latency_ms INTEGER NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The single-use reservation scan (rule d): "does this card already have an APPROVE?".
CREATE INDEX IF NOT EXISTS idx_agent_card_auth_decisions_card_decision
    ON agent_card_auth_decisions (card_id, decision);


-- MERCHANT DESCRIPTOR REGISTRY — which acquirer descriptors belong to a merchant_domain.
--
-- The problem this solves: a card minted for shop.example.com must not be spendable at an
-- unrelated merchant, but the card network never tells us a domain. It tells us a DESCRIPTOR
-- ("ACME STORE", "Berlin", "DE", MCC 5732), and the mapping from descriptor to domain does not
-- exist anywhere we can look it up.
--
-- So the registry is LEARNED, and the first authorization for a domain teaches it. That is a
-- real weakening of the merchant lock and it is stated here rather than hidden: for a domain
-- with no pins, the FIRST authorization is approved on the strength of the other constraints
-- alone (the cap, single use, the expiry, the channel) and its descriptor is pinned. Every
-- later authorization for that domain must match a pin. The exposure that buys is bounded by
-- exactly one authorization, at or below the cap, on a card that expires.
--
-- country is stored as '' rather than NULL when the authorization omits it. Postgres treats
-- NULLs as DISTINCT in a UNIQUE constraint, so a nullable country would make ON CONFLICT never
-- fire and let the same descriptor be pinned without bound.
CREATE TABLE IF NOT EXISTS agent_card_merchant_descriptors (
    id BIGSERIAL PRIMARY KEY,

    merchant_domain VARCHAR(255) NOT NULL,

    -- Normalized descriptor: casefolded, truncated at the first '*', stripped of punctuation,
    -- whitespace collapsed. services/reap_external_auth.normalize_descriptor is the ONE
    -- implementation; a second one would silently un-pin every merchant.
    name_norm TEXT NOT NULL,
    country VARCHAR(2),

    -- Recorded, not matched on. City varies across a merchant's fulfilment sites far more than
    -- the descriptor does, so matching on it would decline honest authorizations.
    city_norm TEXT,

    -- 'authorization' (learned from a live decision) | 'webhook' (a follow-up will also pin
    -- from CARD_TRANSACTION_CREATED's merchant object) | 'manual' (an operator pin).
    source VARCHAR(16) NOT NULL,

    seen_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (merchant_domain, name_norm, country)
);

-- The lookup every decision runs: all pins for this card's merchant_domain.
CREATE INDEX IF NOT EXISTS idx_agent_card_merchant_descriptors_domain
    ON agent_card_merchant_descriptors (merchant_domain);

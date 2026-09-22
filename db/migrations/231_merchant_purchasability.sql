-- A MERCHANT CARRIES A PURCHASE AFFORDANCE ONLY WHILE IT HOLDS A FRESH, POSITIVE PURCHASABILITY
-- FACT. "Unverifiable" is not positive. A frozen row keeps browse and referral; it never keeps buy.
--
-- THE INCIDENT THIS CLOSES (2026-09-22). flowerbeauty.com was served as purchasable. Four
-- separate signals said yes and not one of them was about paying:
--   * the liveness sweep read products.json over HTTP and treated "cannot verify" as a FREEZE
--     that kept every affordance, buy included;
--   * the UCP reprobe recorded `ready_for_complete`, which means the door PRICED a cart — it
--     says nothing whatever about payment;
--   * `/.well-known/ucp` `payment_handlers` listed `dev.shopify.card`, which is a PLATFORM
--     CONSTANT every Shopify store repeats, not an accept-list;
--   * the cart-link preflight rendered the landed checkout and read the merchandise line, the
--     click id and the market — and never looked at the payment methods.
-- Rendered in a browser that day, flowerbeauty's one-page checkout offered PayPal ONLY, and its
-- storefront charged USD 8.00 against our indexed USD 14.95.
--
-- SO THE FACT THIS TABLE HOLDS IS NARROW AND POSITIVE: a checkout we rendered, whose own
-- `availablePaymentLines` named a card gateway with card brands, at the price we think it is.
-- `card_available` is THREE-VALUED and NULL is never FALSE: an unreadable page, a bot challenge
-- or a reset connection is unverifiable, and this rail must never convert "cannot tell" into a
-- negative — that is the mirror of the mistake above, and it would demote good merchants on a
-- proxy flake.
--
-- VANTAGE IS PART OF THE KEY, NOT A LABEL. Reachability is EGRESS-DEPENDENT, measured: judydoll.com
-- RESET direct TCP connections from one of our egresses (3/3) while answering through another
-- (3/3), and a human could not open flowerbeauty.com from his browser at all while our machine
-- could. A fact gathered from the worker's egress is a fact about THE WORKER'S EGRESS. The reader
-- (`is_purchasable`) therefore requires a positive fact from the vantage named by
-- MERCHANT_PURCHASABILITY_BUYER_VANTAGE, which an operator must set to match the buyer/partner
-- egress. A row from another vantage is evidence, not permission.
--
-- DEMOTION IS ON CONFIRMED NEGATIVES ONLY. `consecutive_failures` advances for NO_CARD_PAYMENT,
-- NOT_ACCEPTING_ORDERS, LOGIN_REQUIRED, VARIANT_GONE and PRICE_DRIFT — the store answering about
-- itself. It NEVER advances for BLOCKED_UNKNOWN or any transport outcome. Two consecutive
-- negatives clear `positive_until`; one positive resets the counter and re-arms the window.
--
--   evidence — BOUNDED, and it holds NO BUYER DATA AND NO PAGE HTML. The sweep runs with
--              buyer=None (the Reap path carries no PII in the link), so there is no buyer to
--              leak; what lands here is the payment-method LABELS, the redirect chain's
--              status/host (already redacted by the preflight), and the verdict detail. A full
--              page body would carry the merchant's own tokens and would make this table the
--              largest thing in the database for no read anybody does.
--
-- Production deploys skip db/migrations/, so every statement here is ALSO in
-- db/schema_guard.ensure_required_schema_light, in both dialect branches, each in its own try.
-- The two builds are compared through the catalog by
-- tests/test_merchant_purchasability_postgres.py.

CREATE TABLE IF NOT EXISTS merchant_purchasability (
    merchant_domain VARCHAR(255) NOT NULL,
    market_country VARCHAR(2) NOT NULL
        CONSTRAINT ck_merchant_purchasability_market
        CHECK (market_country ~ '^[A-Z]{2}$'),
    vantage VARCHAR(32) NOT NULL,
    checked_at TIMESTAMPTZ,
    verdict VARCHAR(32),
    card_available BOOLEAN,
    payment_methods JSONB,
    landed_price_minor BIGINT,
    landed_currency VARCHAR(8),
    expected_price_minor BIGINT,
    price_drift_minor BIGINT,
    variant_id VARCHAR(32),
    evidence JSONB,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    positive_until TIMESTAMPTZ,
    PRIMARY KEY (merchant_domain, market_country, vantage)
);

-- The sweep's due-list: oldest first, and a row never checked comes first of all.
CREATE INDEX IF NOT EXISTS idx_merchant_purchasability_due
    ON merchant_purchasability (checked_at);

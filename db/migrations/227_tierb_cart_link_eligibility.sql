-- TIER B CART-LINK ELIGIBILITY — which Shopify merchants accept our cart-permalink checkout today.
--
-- One row per (shop_domain, market), written once a day by jobs/tierb_cart_link_eligibility.py,
-- a standalone Cloud Run Job on the crawl subnet (never the worker, whose egress is the address
-- payment partners allowlist). The job runs services/shopify_cart_link_preflight.preflight with
-- NO buyer and records the verdict here. db/tierb_cart_link_eligibility.py is the only writer.
--
-- A ROW IS A VERDICT PLUS THE LAST ATTEMPT, AND THE TWO ARE DIFFERENT CLOCKS.
--   checked_at       when the last DEFINITE verdict was observed. It is the freshness clock the
--                    purchase flow reads (is_cart_link_eligible) and it moves ONLY with a verdict.
--   last_attempt_at  when the job last tried, whatever happened.
-- An INDEFINITE result (a transport error, an unverifiable variant, an unclassified landing, a
-- refused input) never overwrites a verdict: it sets last_attempt_at and last_error_code and
-- nothing else, so yesterday's ELIGIBLE keeps yesterday's checked_at and ages out on its own
-- instead of being replaced by a non-answer. A merchant whose first attempt was indefinite gets a
-- row with verdict NULL, which reads as not eligible.
--
-- The CHECKs below hold that shape in the database, not only in the module: `verdict` and
-- `previous_verdict` can only ever carry a DEFINITE verdict, and `verdict` is NULL exactly when
-- `checked_at` is.
--
-- NO PII. The job never passes a buyer, and nothing here could hold one. `detail` and
-- `last_error_code` carry the preflight's own codes, never a URL.
--
-- Production deploys skip db/migrations/, so the same table is created by the schema-guard
-- self-heal (db/tierb_cart_link_eligibility_schema.ensure_schema, called from
-- db/schema_guard.ensure_required_schema_light). The two must build the SAME schema, which
-- tests/test_tierb_cart_link_eligibility_postgres.py checks through the catalog (columns,
-- index definitions, CHECK definitions). If you change one, change both.

CREATE TABLE IF NOT EXISTS tierb_cart_link_eligibility (
    id BIGSERIAL PRIMARY KEY,

    -- Lowercased, leading `www.` stripped (db/tierb_cart_link_eligibility.normalize_domain), on
    -- write AND on read, so www.Judydoll.com and judydoll.com are one row.
    shop_domain VARCHAR(255) NOT NULL,

    -- The BUYER market availability was read for (ISO-2, upper case).
    market VARCHAR(2) NOT NULL CHECK (length(market) = 2 AND market = upper(market)),

    -- The last DEFINITE verdict, or NULL when no attempt has produced one yet.
    verdict VARCHAR(32) CHECK (verdict IS NULL OR verdict IN (
        'ELIGIBLE', 'LOGIN_REQUIRED', 'NOT_ACCEPTING_ORDERS', 'VARIANT_GONE',
        'VARIANT_UNAVAILABLE', 'PASSWORD_PAGE', 'BLOCKED_UNKNOWN', 'CHECKOUT_PREFILL_MISSING'
    )),
    retryable BOOLEAN,

    -- What the definite verdict was observed on. Evidence, from the public storefront.
    variant_id VARCHAR(32),
    variant_source VARCHAR(16),
    product_title TEXT,
    price_text VARCHAR(32),
    detail VARCHAR(255),

    checked_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error_code VARCHAR(128),

    -- History, one step deep: when the verdict last changed, what it was before, and how many
    -- consecutive definite runs have returned the current one.
    verdict_changed_at TIMESTAMPTZ,
    previous_verdict VARCHAR(32) CHECK (previous_verdict IS NULL OR previous_verdict IN (
        'ELIGIBLE', 'LOGIN_REQUIRED', 'NOT_ACCEPTING_ORDERS', 'VARIANT_GONE',
        'VARIANT_UNAVAILABLE', 'PASSWORD_PAGE', 'BLOCKED_UNKNOWN', 'CHECKOUT_PREFILL_MISSING'
    )),
    consecutive_same INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_same >= 0),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_tierb_cart_link_eligibility_verdict_has_clock
        CHECK ((verdict IS NULL) = (checked_at IS NULL)),
    CONSTRAINT uq_tierb_cart_link_eligibility_shop_market UNIQUE (shop_domain, market)
);

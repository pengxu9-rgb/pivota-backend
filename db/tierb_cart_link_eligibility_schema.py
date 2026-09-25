"""The DDL for migration 228's table, as the schema-guard self-heal runs it.

A module of its own, importing nothing but db.database, because db/schema_guard imports it at
startup inside a try that swallows every exception: an import chain that failed there (the
eligibility module pulls in the preflight, which pulls in the outbound-links service) would
leave the table silently uncreated. Here nothing can fail on import.
"""

from __future__ import annotations

from db.database import IS_POSTGRES, database

# ── schema (the self-heal) ──────────────────────────────────────────────────────────────────
#
# Production never runs db/migrations, so this IS the production schema: db/schema_guard.
# ensure_required_schema_light calls `ensure_schema` early, in its own try. The Postgres DDL
# must build the same schema as db/migrations/228_tierb_cart_link_eligibility.sql —
# tests/test_tierb_cart_link_eligibility_postgres.py compares the two through the catalog.
# The SQLite twin differs only where SQLite must (no now(), no BIGSERIAL, no TIMESTAMPTZ);
# every CHECK, NOT NULL and the UNIQUE key are the same.

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tierb_cart_link_eligibility (
    id BIGSERIAL PRIMARY KEY,
    shop_domain VARCHAR(255) NOT NULL,
    market VARCHAR(2) NOT NULL CHECK (length(market) = 2 AND market = upper(market)),
    verdict VARCHAR(32) CHECK (verdict IS NULL OR verdict IN (
        'ELIGIBLE', 'LOGIN_REQUIRED', 'NOT_ACCEPTING_ORDERS', 'VARIANT_GONE',
        'VARIANT_UNAVAILABLE', 'PASSWORD_PAGE', 'BLOCKED_UNKNOWN', 'CHECKOUT_PREFILL_MISSING',
        'CHECKOUT_MARKET_MISMATCH', 'NO_CARD_PAYMENT', 'PRICE_DRIFT'
    )),
    retryable BOOLEAN,
    variant_id VARCHAR(32),
    variant_source VARCHAR(16),
    product_title TEXT,
    price_text VARCHAR(32),
    detail VARCHAR(255),
    checkout_country VARCHAR(2) CHECK (checkout_country IS NULL OR (length(checkout_country) = 2 AND checkout_country = upper(checkout_country))),
    checked_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error_code VARCHAR(128),
    verdict_changed_at TIMESTAMPTZ,
    previous_verdict VARCHAR(32) CHECK (previous_verdict IS NULL OR previous_verdict IN (
        'ELIGIBLE', 'LOGIN_REQUIRED', 'NOT_ACCEPTING_ORDERS', 'VARIANT_GONE',
        'VARIANT_UNAVAILABLE', 'PASSWORD_PAGE', 'BLOCKED_UNKNOWN', 'CHECKOUT_PREFILL_MISSING',
        'CHECKOUT_MARKET_MISMATCH', 'NO_CARD_PAYMENT', 'PRICE_DRIFT'
    )),
    consecutive_same INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_same >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_tierb_cart_link_eligibility_verdict_has_clock
        CHECK ((verdict IS NULL) = (checked_at IS NULL)),
    CONSTRAINT uq_tierb_cart_link_eligibility_shop_market UNIQUE (shop_domain, market)
)
"""

_CREATE_TABLE_SQL_SQLITE = """
CREATE TABLE IF NOT EXISTS tierb_cart_link_eligibility (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_domain VARCHAR(255) NOT NULL,
    market VARCHAR(2) NOT NULL CHECK (length(market) = 2 AND market = upper(market)),
    verdict VARCHAR(32) CHECK (verdict IS NULL OR verdict IN (
        'ELIGIBLE', 'LOGIN_REQUIRED', 'NOT_ACCEPTING_ORDERS', 'VARIANT_GONE',
        'VARIANT_UNAVAILABLE', 'PASSWORD_PAGE', 'BLOCKED_UNKNOWN', 'CHECKOUT_PREFILL_MISSING',
        'CHECKOUT_MARKET_MISMATCH', 'NO_CARD_PAYMENT', 'PRICE_DRIFT'
    )),
    retryable BOOLEAN,
    variant_id VARCHAR(32),
    variant_source VARCHAR(16),
    product_title TEXT,
    price_text VARCHAR(32),
    detail VARCHAR(255),
    checkout_country VARCHAR(2) CHECK (checkout_country IS NULL OR (length(checkout_country) = 2 AND checkout_country = upper(checkout_country))),
    checked_at TIMESTAMP,
    last_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error_code VARCHAR(128),
    verdict_changed_at TIMESTAMP,
    previous_verdict VARCHAR(32) CHECK (previous_verdict IS NULL OR previous_verdict IN (
        'ELIGIBLE', 'LOGIN_REQUIRED', 'NOT_ACCEPTING_ORDERS', 'VARIANT_GONE',
        'VARIANT_UNAVAILABLE', 'PASSWORD_PAGE', 'BLOCKED_UNKNOWN', 'CHECKOUT_PREFILL_MISSING',
        'CHECKOUT_MARKET_MISMATCH', 'NO_CARD_PAYMENT', 'PRICE_DRIFT'
    )),
    consecutive_same INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_same >= 0),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_tierb_cart_link_eligibility_verdict_has_clock
        CHECK ((verdict IS NULL) = (checked_at IS NULL)),
    CONSTRAINT uq_tierb_cart_link_eligibility_shop_market UNIQUE (shop_domain, market)
)
"""


async def ensure_schema() -> None:
    """Create the table if it is missing. Idempotent; raises on failure (the schema guard wraps
    it in its own try, so a failure here cannot starve the self-heals after it)."""
    if IS_POSTGRES:
        await database.execute(_CREATE_TABLE_SQL)
    else:
        await database.execute(_CREATE_TABLE_SQL_SQLITE)

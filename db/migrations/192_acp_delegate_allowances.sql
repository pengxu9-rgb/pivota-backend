-- 192: acp_delegate_allowances — the ACP delegated-token ALLOWANCE registry.
--
-- WHY THIS EXISTS. ACP's design method for external agents is *delegated
-- payment*: the platform (OpenAI) hands the business a single-use,
-- amount-capped, merchant-scoped token instead of a raw PSP token. Pivota is
-- NOT a card vault and never will be (see the P1 design,
-- PIVOTA_ACP_DELEGATED_TOKEN_EXCHANGE_DESIGN_2026-08-04): we never implement
-- `POST /agentic_commerce/delegate_payment`, never receive cardholder data, and
-- never store payment-method material. What we DO keep is our own recorded view
-- of an allowance a delegated token was minted under, so `complete_session` can
-- enforce it locally, fail-closed, IN ADDITION to whatever the PSP enforces
-- authoritatively on its side.
--
--   token_id            the `vt_<14 hex>` delegate token id (wire-parity format
--                       with the retired pivota-acp service). PRIMARY KEY: a
--                       token is its own identity, and the CAS consumption
--                       below needs exactly one row to contend for.
--   checkout_session_id the acp_checkout_sessions.id the allowance was minted
--                       FOR. A token presented at any other session is refused
--                       (`allowance_session_mismatch`).
--   merchant_id         the merchant the allowance is scoped to; must equal the
--                       session's merchant (`allowance_merchant_mismatch`).
--                       The retired service never checked this — a token minted
--                       for merchant A was spendable at merchant B.
--   max_amount          minor units. The SERVER-SIDE session total must be
--                       <= this (equality passes — wire parity with the retired
--                       service's `total > max_amount` refusal). Never compared
--                       against a caller-supplied amount.
--   currency            the allowance's currency; compared case-insensitively
--                       against the SESSION's currency
--                       (`allowance_currency_mismatch`).
--   reason              the delegation reason; only 'one_time' is minted or
--                       accepted today (the column exists because the spec's
--                       allowance object carries it, not because we support
--                       recurring delegation).
--   expires_at          NOT NULL, and actually CHECKED at completion
--                       (`allowance_expired`). The retired service stored this
--                       column and never once read it.
--   used / used_at / used_by_session
--                       SINGLE-USE consumption, claimed by the same conditional
--                       -UPDATE-with-RETURNING technique as the session claim:
--                       `SET used=TRUE, used_by_session=:sid
--                        WHERE token_id=:t AND (used=FALSE OR used_by_session=:sid)`.
--                       Exactly one session can ever bind a token; re-binding by
--                       the SAME session is idempotent on purpose (a retry or a
--                       stale-resume of that session's completion must not be
--                       refused by its own earlier bind), and any OTHER session
--                       is refused (`allowance_already_used`). The retired
--                       service had no consumption marking at all: its tokens
--                       were reusable forever.
--
-- NO CARDHOLDER DATA, BY CONSTRUCTION. There is deliberately NO column for a
-- PAN/number, CVC, cryptogram, expiry, or even a display last4/brand/IIN
-- (founder call: drop the display fields until a UI actually needs them). The
-- retired pivota-acp `delegate_payment` stored the entire request — raw PAN and
-- CVC — unencrypted in a JSONB payload column; that is a PCI Req 3.2 violation
-- by design, and CVC storage is prohibited outright. A schema-guard test
-- asserts no column of this table matches number|cvc|pan|cryptogram, so the
-- absence is enforced, not merely intended.
--
-- Idempotent by construction (IF NOT EXISTS everywhere): safe to re-run, and
-- the registry service self-heals the same schema at runtime for environments
-- where migrations cannot be run manually (see
-- _ensure_acp_delegate_allowances_table).
--
-- schema-guard-exempt: acp_delegate_allowances carries its OWN runtime
-- self-heal rather than a db/schema_guard.py entry — the same arrangement as
-- 191_acp_checkout_sessions. Every access path in
-- services/acp_delegate_allowance_service (mint, get, CAS bind) runs this exact
-- DDL through _ensure_acp_delegate_allowances_table on a missing/drifted table
-- and retries, so a Railway deploy that skips db/migrations/ still converges on
-- the first request that touches the table. Duplicating the DDL into
-- ensure_required_schema_light would give two sources of truth for a money-path
-- table with no added safety.

CREATE TABLE IF NOT EXISTS acp_delegate_allowances (
    token_id TEXT PRIMARY KEY,
    checkout_session_id TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    max_amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT 'one_time',
    expires_at TIMESTAMPTZ NOT NULL,
    used BOOLEAN NOT NULL DEFAULT FALSE,
    used_at TIMESTAMPTZ,
    used_by_session TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_acp_delegate_allowances_session
    ON acp_delegate_allowances (checkout_session_id);

-- 191: acp_checkout_sessions — backend-owned ACP checkout-session storage.
--
-- WHY THIS EXISTS. The external `pivota-acp` Railway service is being retired;
-- the backend now executes the ACP checkout-session flow in-process
-- (services/acp_checkout_session_service.py). This table replaces the retired
-- service's `checkout_sessions` table, modeled on its columns but backend-owned:
--
--   id                 csn_<hex> session id (same format as the old service)
--   agent_id           the agent identity that minted the session; completion
--                      requires the same agent (enforced in the service)
--   status             ready_for_payment -> completing -> completed.
--                      'completing' is the single-flight CLAIM: exactly one
--                      completion attempt holds it (claimed via a conditional
--                      UPDATE), so concurrent /complete calls cannot both
--                      charge. A capture failure reverts to ready_for_payment.
--   items              the cart as submitted (sku/product_id/variant_id/quantity)
--   quote              the in-process Pivota quote snapshot taken at create time
--   metadata           pvt_* attribution + caller metadata (survives to /complete)
--   order_id           persisted IMMEDIATELY after order creation, BEFORE the
--                      charge — a retry reuses this order, never mints another
--   completion         the stored /complete result, replayed idempotently so a
--                      retry can NEVER double-charge
--   idempotency_key    caller-supplied /complete idempotency key. Recorded and
--                      validated only (409 on reuse with a different payload);
--                      the PSP idempotency key is DERIVED from the session id,
--                      deliberately NOT from this value, so a retry with a
--                      fresh key cannot double-charge. Indexed non-unique: the
--                      service answers cross-session reuse with a SELECT + 409
--                      BEFORE charging (a unique index here would abort the
--                      completion write only AFTER the charge).
--   expires_at         create-time + TTL (default 3600s); expired sessions are
--                      treated as absent
--
-- Idempotent by construction (IF NOT EXISTS everywhere): safe to re-run, and
-- the service self-heals the same schema at runtime for environments where
-- migrations cannot be run manually (see _ensure_acp_checkout_sessions_table).

CREATE TABLE IF NOT EXISTS acp_checkout_sessions (
    id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    agent_id TEXT,
    platform TEXT,
    status TEXT NOT NULL DEFAULT 'ready_for_payment',
    buyer JSONB,
    items JSONB NOT NULL,
    fulfillment_address JSONB,
    quote JSONB,
    metadata JSONB,
    currency TEXT,
    total_cents INTEGER,
    order_id TEXT,
    completion JSONB,
    idempotency_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ
);

-- Schema drift healing for a table pre-created by an older build.
ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS completion JSONB;
ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS order_id TEXT;
ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_acp_checkout_sessions_merchant_created
    ON acp_checkout_sessions (merchant_id, created_at);

CREATE INDEX IF NOT EXISTS idx_acp_checkout_sessions_merchant_idem
    ON acp_checkout_sessions (merchant_id, idempotency_key);

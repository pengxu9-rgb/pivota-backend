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
--                      charge. A DEFINITIVE capture failure (the PSP refused,
--                      or nothing was dispatched) reverts to
--                      ready_for_payment; an AMBIGUOUS one (timeout/transport)
--                      stays `completing` so the stale-resume path replays the
--                      stored psp_idempotency_key rather than re-charging.
--   items              the cart as submitted (sku/product_id/variant_id/quantity)
--   quote              the in-process Pivota quote snapshot taken at create time
--   metadata           pvt_* attribution + caller metadata (survives to /complete)
--   order_id           persisted IMMEDIATELY after order creation, BEFORE the
--                      charge — a retry reuses this order, never mints another
--   completion         the stored /complete result, replayed idempotently so a
--                      retry can NEVER double-charge
--   idempotency_key    caller-supplied /complete idempotency key. Recorded and
--                      validated only (409 on reuse with a different payload);
--                      the PSP idempotency key is STORED on the row (see
--                      psp_idempotency_key), deliberately NOT taken from this
--                      value, so a retry with a fresh key cannot double-charge.
--                      Indexed non-unique: the service answers cross-session
--                      reuse with a SELECT + 409 BEFORE charging (a unique
--                      index here would abort the completion write only AFTER
--                      the charge).
--   capture_attempt    incremented by the CLAIM UPDATE on every FRESH claim
--                      (ready_for_payment -> completing). Resuming a stale
--                      `completing` row never increments it.
--   psp_idempotency_key
--                      the key the PSP charge for the CURRENT attempt runs
--                      under — `acp_complete:<session_id>:a<capture_attempt>`,
--                      written by the same claim UPDATE BEFORE any order or
--                      capture work. Attempt-scoped on purpose: a resumed
--                      completion replays the STORED key (so a lost PSP
--                      response can never become a second charge), while a
--                      definitively DECLINED completion reverts to
--                      ready_for_payment and the next claim mints a new key
--                      (so the buyer can retry with another card instead of
--                      replaying a cached decline forever).
--   expires_at         create-time + TTL (default 3600s); expired sessions are
--                      treated as absent
--
-- Idempotent by construction (IF NOT EXISTS everywhere): safe to re-run, and
-- the service self-heals the same schema at runtime for environments where
-- migrations cannot be run manually (see _ensure_acp_checkout_sessions_table).
--
-- schema-guard-exempt: acp_checkout_sessions carries its OWN runtime self-heal
-- rather than a db/schema_guard.py entry. Every access path in
-- services/acp_checkout_session_service (create, peek, claim, stale-takeover)
-- runs its DDL through _ensure_acp_checkout_sessions_table on a missing/drifted
-- table and retries — the exact CREATE TABLE + ADD COLUMN IF NOT EXISTS set
-- below, kept in lockstep with this file. A Railway deploy that skips
-- db/migrations/ therefore still converges on the first request that touches
-- the table, including these columns. Duplicating the DDL into
-- ensure_required_schema_light would give two sources of truth for a money-path
-- table with no added safety.

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
    capture_attempt INTEGER NOT NULL DEFAULT 0,
    psp_idempotency_key TEXT,
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
ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS capture_attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS psp_idempotency_key TEXT;

CREATE INDEX IF NOT EXISTS idx_acp_checkout_sessions_merchant_created
    ON acp_checkout_sessions (merchant_id, created_at);

CREATE INDEX IF NOT EXISTS idx_acp_checkout_sessions_merchant_idem
    ON acp_checkout_sessions (merchant_id, idempotency_key);

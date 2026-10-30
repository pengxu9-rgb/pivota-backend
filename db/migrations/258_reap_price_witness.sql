-- 258: THE PRICE WITNESS on the purchase row (buy-intent preflight quote + corroborated price
-- change). Owner decision 2026-10-05. Written ONLY by db/reap_price_witness.py, and only while
-- REAP_AGENTIC_PREFLIGHT_MODE (shadow|enforce) or REAP_AGENTIC_PRICE_CORROBORATION (on) is
-- armed: with both dials off nothing reads or writes these columns, and `transition` /
-- `create_purchase` do not name them, so a database missing them still opens and advances
-- purchases exactly as before.
--
--   preflight_outcome       pending | ok | price_changed | refused | unverified. The outcome of
--                           the ONE buy-intent quote taken in 'resolving' before the card step.
--                           'pending' is written BEFORE the partner call (intent first), so a
--                           worker that dies mid-call leaves a marker the next tick reads as
--                           "already quoted" and never quotes a second time.
--   preflight_error_code    the check's or the provider's code (our lowercase vocabulary).
--   preflight_checked_at    when the witness was taken.
--   preflight_items_subtotal_minor, preflight_shipping_minor, preflight_tax_minor,
--   preflight_tax_included, preflight_total_minor
--                           the CONFIRMED totals of an 'ok' witness (NULL otherwise). The
--                           witness quote id is never stored: it is never used for checkout.
--   live_unit_price_minor, live_items_subtotal_minor, live_quoted_total_minor, live_price_stage
--                           the merchant's LIVE price as Reap last quoted it, when that quote's
--                           items subtotal differed from ours (stage 'preflight' | 'approval').
--                           live_unit_price_minor is NULL when the subtotal is not an exact
--                           multiple of the quantity.
--   price_rebound_from_minor, price_rebound_to_minor, price_corroboration_source,
--   price_corroborated_at   a LOWER live unit price that our own independent store read
--                           corroborated, so the purchase continued at it. `our_price_minor` is
--                           NOT rewritten: it stays the price the buyer selected (and the money
--                           pair bound into the attempt's idempotency fingerprint).
--
-- NULLABLE, no default, no CHECK (the vocabulary is enforced by the writer, as for
-- offer_code_outcome in 247, so the SQLite twin needs no table rebuild). Nothing here is buyer
-- PII; the terminal write leaves them.
--
-- Production deploys skip db/migrations/, so db/reap_price_witness.ensure_price_witness_schema
-- (called from db/schema_guard in both dialect branches) adds the same columns.

ALTER TABLE IF EXISTS reap_agentic_purchases
    ADD COLUMN IF NOT EXISTS preflight_outcome VARCHAR(16),
    ADD COLUMN IF NOT EXISTS preflight_error_code VARCHAR(64),
    ADD COLUMN IF NOT EXISTS preflight_checked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS preflight_items_subtotal_minor BIGINT,
    ADD COLUMN IF NOT EXISTS preflight_shipping_minor BIGINT,
    ADD COLUMN IF NOT EXISTS preflight_tax_minor BIGINT,
    ADD COLUMN IF NOT EXISTS preflight_tax_included BOOLEAN,
    ADD COLUMN IF NOT EXISTS preflight_total_minor BIGINT,
    ADD COLUMN IF NOT EXISTS live_unit_price_minor BIGINT,
    ADD COLUMN IF NOT EXISTS live_items_subtotal_minor BIGINT,
    ADD COLUMN IF NOT EXISTS live_quoted_total_minor BIGINT,
    ADD COLUMN IF NOT EXISTS live_price_stage VARCHAR(16),
    ADD COLUMN IF NOT EXISTS price_rebound_from_minor BIGINT,
    ADD COLUMN IF NOT EXISTS price_rebound_to_minor BIGINT,
    ADD COLUMN IF NOT EXISTS price_corroboration_source VARCHAR(32),
    ADD COLUMN IF NOT EXISTS price_corroborated_at TIMESTAMPTZ;

-- 256: WHEN THE FIRST CHECKOUT CREATE FOR THE IN-FLIGHT QUOTE WAS SENT, on the purchase row.
--
-- services/reap_agentic_purchase replays `POST /agentic/checkouts` for a 'quoting' row whose
-- create outcome is unknown (`reap_quote_id` set, no checkout id) on the SAME Idempotency-Key,
-- (quoteId, enrollmentId), so Reap answers with the checkout it created rather than a second one.
-- Reap keeps an idempotency key for about 24 hours. After that a replay is a FRESH request: it
-- would be refused QUOTE_EXPIRED, which we would read as "nothing was created" and re-quote --
-- a second checkout beside one that may exist. So the row records when the first create was
-- sent (db/reap_agentic_ledger.record_checkout_attempt, server clock, written once and never
-- moved by a re-record of the same quote), and past `CHECKOUT_REPLAY_WINDOW_SECONDS` (23 h) the
-- purchase stops replaying and is parked for a human instead.
--
-- NULL = no create in flight. Cleared with the marker (`clear_checkout_attempt`) on a definitive
-- "nothing was created". Internal: not in PUBLIC_PURCHASE_COLUMNS.
--
-- Production deploys skip db/migrations/, so the same column is ALSO in
-- db/schema_guard.ensure_required_schema_light, in both dialect branches.
ALTER TABLE IF EXISTS reap_agentic_purchases
    ADD COLUMN IF NOT EXISTS checkout_create_sent_at TIMESTAMPTZ;

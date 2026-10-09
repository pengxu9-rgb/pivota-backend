-- 247: THE BUYER'S OFFER CODE, AND WHAT IT CAME TO, on the purchase row.
--
-- Reap's 2026-09-28 spec added an optional `offerCode` to BOTH quote branches. A purchase is
-- opened by the route and QUOTED by the poller in another process, so a code the buyer typed
-- has to be on the row to reach the quote at all. And Reap can refuse a code (400
-- OFFER_CODE_INVALID / OFFER_CODE_EXPIRED); the purchase then re-quotes ONCE without it
-- (services/reap_agentic_purchase._quote_with_offer_code), and the buyer must be told that
-- before approving a price that is not the one they expected. So four columns:
--
--   offer_code          the code as the buyer gave it (1..128 chars, never case-folded or
--                       trimmed -- see services.reap_agentic_client.validate_offer_code, the one
--                       rule the route, the service and the ledger all call). CREATE-ONLY: it is
--                       not in db/reap_agentic_ledger._TRANSITION_FIELDS, so no poller step can
--                       revise what the buyer asked for.
--   offer_code_outcome  'applied' | 'no_discount' | 'dropped_invalid' | 'dropped_expired',
--                       written by the 'quoting' step. NULL = no code, or not quoted yet. The
--                       ledger refuses any other value (OFFER_CODE_OUTCOMES) before the UPDATE;
--                       there is no CHECK here so the SQLite twin and this build the same
--                       schema without a table rebuild.
--   tax_included        Reap's `tax.includedInPrices` for this quote: TRUE when `tax_minor` is
--                       already inside the prices (live jsmbeauty.sg SGD quote, 2026-09-28), so a
--                       reader summing subtotal + shipping + tax does not count it twice. NULL =
--                       not quoted yet.
--   discount_minor      the MAGNITUDE of Reap's `discounts[]` lines, minor units, >= 0.
--                       EVIDENCE, never an input: the amount charged is Reap's `finalAmount`
--                       (quoted_total_minor), and the verifier only checks the lines reconcile.
--
-- BUYER-ENTERED, SO SCRUBBED LIKE THE EMAIL AND THE ADDRESS: every terminal write (the transition
-- and both sweeps) NULLs `offer_code`. `offer_code_outcome`, `discount_minor` and `tax_included`
-- are facts about the quote, not buyer input, and survive it. All four are in
-- PUBLIC_PURCHASE_COLUMNS.
--
-- NULLABLE, no default: rows opened before this migration had no code, and NULL says exactly
-- that.
--
-- Production deploys skip db/migrations/, so the same four columns are ALSO in
-- db/schema_guard.ensure_required_schema_light, in BOTH dialect branches (one multi-clause
-- statement on Postgres, one statement PER COLUMN on SQLite, each in its own try). The
-- whole-table parity test in tests/test_reap_agentic_ledger_postgres.py reads what the
-- database built from each, so a divergence is a failure there rather than a surprise in
-- production.
--
-- ORDERING WITH CODE. db/reap_agentic_ledger.py names `offer_code` in every purchase INSERT and
-- `offer_code_outcome` / `discount_minor` in every transition, so a database without these
-- columns fails LOUDLY (UndefinedColumn) on the first purchase write -- before any partner call
-- and before any money moves -- rather than silently dropping a buyer's code.

ALTER TABLE IF EXISTS reap_agentic_purchases
    ADD COLUMN IF NOT EXISTS offer_code VARCHAR(128),
    ADD COLUMN IF NOT EXISTS offer_code_outcome VARCHAR(16),
    ADD COLUMN IF NOT EXISTS discount_minor BIGINT,
    ADD COLUMN IF NOT EXISTS tax_included BOOLEAN;

-- Widen migration 228's verdict vocabulary for the two members the purchasability work adds to
-- the preflight's `Verdict` enum: NO_CARD_PAYMENT and PRICE_DRIFT.
--
-- WHY THIS IS NEEDED AT ALL. db/tierb_cart_link_eligibility.py must classify EVERY member of that
-- enum as definite or indefinite — `unclassified_verdicts()` and its test exist to force the
-- decision — and the DEFINITE set is exactly the vocabulary these two CHECKs admit. So adding a
-- member to the enum and classifying it definite without widening the CHECK would write a row the
-- database refuses.
--
-- AND THEY ARE CLASSIFIED DEFINITE, NOT INDEFINITE, DELIBERATELY. The Tier B job does not produce
-- either verdict today: it calls the preflight without `check_card` and without an expected price,
-- so neither can be reached from that lane. The classification therefore costs nothing now and is
-- a choice about what happens if somebody later turns the card check on for that job. Indefinite
-- would mean "never overwrite a verdict", so a merchant that went PayPal-only would KEEP its stale
-- ELIGIBLE and keep sending buyers to a checkout they cannot pay. That is fail-OPEN on the exact
-- failure this whole change exists to stop. Definite is fail-closed.
--
-- THE CONSTRAINT NAMES ARE POSTGRES'S OWN. Migration 228 declared both CHECKs inline and
-- unnamed, so Postgres named them `<table>_<column>_check`. They are dropped by that name and
-- re-added under the same one, which keeps the self-heal's CREATE TABLE — where the widened list
-- is again an inline, unnamed CHECK that Postgres will name identically — byte-identical to what
-- this migration leaves behind. tests/test_tierb_cart_link_eligibility_postgres.py compares the
-- two builds through the catalog and is what proves that.
--
-- ORDER WITHIN THE LISTS IS NOT SIGNIFICANT TO THE DATABASE but IS significant to the parity
-- test, which compares `pg_get_constraintdef` text. Keep these lists in the same order as the
-- ones in db/tierb_cart_link_eligibility_schema.py.
--
-- schema-guard-exempt: this migration adds no column. Its effect on a FRESH database is already
-- carried by the widened CREATE TABLE in db/tierb_cart_link_eligibility_schema.py, which the
-- schema guard runs; on an EXISTING database this file is what widens the constraint.

ALTER TABLE tierb_cart_link_eligibility
    DROP CONSTRAINT IF EXISTS tierb_cart_link_eligibility_verdict_check;

ALTER TABLE tierb_cart_link_eligibility
    ADD CONSTRAINT tierb_cart_link_eligibility_verdict_check
    CHECK (verdict IS NULL OR verdict IN (
        'ELIGIBLE', 'LOGIN_REQUIRED', 'NOT_ACCEPTING_ORDERS', 'VARIANT_GONE',
        'VARIANT_UNAVAILABLE', 'PASSWORD_PAGE', 'BLOCKED_UNKNOWN', 'CHECKOUT_PREFILL_MISSING',
        'CHECKOUT_MARKET_MISMATCH', 'NO_CARD_PAYMENT', 'PRICE_DRIFT'
    ));

ALTER TABLE tierb_cart_link_eligibility
    DROP CONSTRAINT IF EXISTS tierb_cart_link_eligibility_previous_verdict_check;

ALTER TABLE tierb_cart_link_eligibility
    ADD CONSTRAINT tierb_cart_link_eligibility_previous_verdict_check
    CHECK (previous_verdict IS NULL OR previous_verdict IN (
        'ELIGIBLE', 'LOGIN_REQUIRED', 'NOT_ACCEPTING_ORDERS', 'VARIANT_GONE',
        'VARIANT_UNAVAILABLE', 'PASSWORD_PAGE', 'BLOCKED_UNKNOWN', 'CHECKOUT_PREFILL_MISSING',
        'CHECKOUT_MARKET_MISMATCH', 'NO_CARD_PAYMENT', 'PRICE_DRIFT'
    ));

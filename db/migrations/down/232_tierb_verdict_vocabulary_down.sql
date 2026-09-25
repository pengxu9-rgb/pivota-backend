-- Reverse of 232_tierb_verdict_vocabulary.sql: narrow the verdict vocabulary back to 228's.
--
-- THIS FAILS IF ANY ROW ALREADY HOLDS ONE OF THE TWO NEW VERDICTS, and that is correct: the
-- constraint cannot be re-narrowed over data it forbids, and silently deleting a merchant's
-- verdict to make a rollback succeed would erase the finding rather than roll it back. Clear or
-- re-check those rows first (see docs/runbooks/merchant_purchasability.md).
ALTER TABLE tierb_cart_link_eligibility
    DROP CONSTRAINT IF EXISTS tierb_cart_link_eligibility_verdict_check;

ALTER TABLE tierb_cart_link_eligibility
    ADD CONSTRAINT tierb_cart_link_eligibility_verdict_check
    CHECK (verdict IS NULL OR verdict IN (
        'ELIGIBLE', 'LOGIN_REQUIRED', 'NOT_ACCEPTING_ORDERS', 'VARIANT_GONE',
        'VARIANT_UNAVAILABLE', 'PASSWORD_PAGE', 'BLOCKED_UNKNOWN', 'CHECKOUT_PREFILL_MISSING',
        'CHECKOUT_MARKET_MISMATCH'
    ));

ALTER TABLE tierb_cart_link_eligibility
    DROP CONSTRAINT IF EXISTS tierb_cart_link_eligibility_previous_verdict_check;

ALTER TABLE tierb_cart_link_eligibility
    ADD CONSTRAINT tierb_cart_link_eligibility_previous_verdict_check
    CHECK (previous_verdict IS NULL OR previous_verdict IN (
        'ELIGIBLE', 'LOGIN_REQUIRED', 'NOT_ACCEPTING_ORDERS', 'VARIANT_GONE',
        'VARIANT_UNAVAILABLE', 'PASSWORD_PAGE', 'BLOCKED_UNKNOWN', 'CHECKOUT_PREFILL_MISSING',
        'CHECKOUT_MARKET_MISMATCH'
    ));

-- 282_merchant_onboarding_is_test.sql
--
-- A staff-set fact: this merchant is one of our own test or demo merchants, not a real seller.
--
-- Until now "test merchant" was a code list (services/test_merchant_policy KNOWN_TEST_MERCHANT_IDS),
-- an env list (PIVOTA_TEST_MERCHANT_IDS) and a demo-domain prefix. A new internal merchant was
-- a real merchant to every reader until someone edited code or env on every service. This column
-- makes it a row: set and cleared by staff through POST /admin/merchants/{merchant_id}/is-test,
-- which requires a reason and writes an employee_audit_logs row in the same transaction.
--
-- Read by services/test_merchant_policy only while MERCHANT_IS_TEST_FILTER_ENABLED is on
-- (default off). With the flag off nothing reads it, so this migration changes no answer.
--
-- On merchant_onboarding because that is the merchant row: orders.merchant_id, the agent
-- merchant list and the protocol directory's domain resolution all key on it.
--
-- NOT NULL DEFAULT FALSE: every existing merchant is real until staff say otherwise. A constant
-- default is a catalog-only change on Postgres 11+ (no table rewrite). Idempotent.
--
-- PROD: migrations do not self-apply. db/schema_guard.py adds the same column at boot
-- (ensure_required_schema_light) and lists it in REQUIRED_SCHEMA, so /health reports it missing
-- until one of the two has run.

ALTER TABLE IF EXISTS merchant_onboarding
  ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN merchant_onboarding.is_test IS
  'Staff-set: this merchant is an internal test or demo merchant. Written only by POST /admin/merchants/{merchant_id}/is-test (reason + employee_audit_logs row). Read by services/test_merchant_policy behind MERCHANT_IS_TEST_FILTER_ENABLED.';

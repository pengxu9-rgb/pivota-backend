-- Drops the staff-set test-merchant fact (migration 282). Turn MERCHANT_IS_TEST_FILTER_ENABLED off and roll
-- the image back first: POST /admin/merchants/{merchant_id}/is-test writes this column and db/schema_guard.py
-- re-adds it on the next boot of any image that still carries its heal (and lists it in REQUIRED_SCHEMA).
ALTER TABLE IF EXISTS merchant_onboarding
  DROP COLUMN IF EXISTS is_test;

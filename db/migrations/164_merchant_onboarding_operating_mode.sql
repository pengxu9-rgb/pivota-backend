-- 164_merchant_onboarding_operating_mode.sql
-- Declared merchant mode: let a store-less brand onboard without a store URL.
--
-- Today merchant_onboarding.store_url is NOT NULL because the storefront path
-- needs it for URL-based auto-KYB and MCP. A store-less brand has no storefront
-- to point at, so we (a) relax the NOT NULL on store_url, and (b) add an
-- explicit operating_mode discriminator:
--   storefront  — DEFAULT; today's behavior, store_url still required (app-enforced)
--   store_less  — declared brand; no store_url, lenient LOW-confidence auto-approve,
--                 gated downstream (products stay un-served until graduated/claimed)
--
-- Purely additive + defaulted: every existing row becomes 'storefront', so the
-- storefront path is byte-identical to today. Idempotent.
-- Prod skips the migration runner — apply via railway ssh / admin.
BEGIN;

ALTER TABLE merchant_onboarding
  ALTER COLUMN store_url DROP NOT NULL;

ALTER TABLE merchant_onboarding
  ADD COLUMN IF NOT EXISTS operating_mode VARCHAR(32) NOT NULL DEFAULT 'storefront';

COMMIT;

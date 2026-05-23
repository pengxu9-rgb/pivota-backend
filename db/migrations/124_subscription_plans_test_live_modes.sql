-- 124_subscription_plans_test_live_modes.sql
-- Allow the subscription_plans catalogue to hold both Stripe Test and
-- Stripe Live entries for the same human-facing plan name.
--
-- Context: prod and staging share one Postgres (single-DB tenancy). The
-- table previously enforced UNIQUE(name) which forced a single price_id
-- per plan name, so the catalogue could only describe ONE Stripe mode at
-- a time. After the 2026-05-22 Live key rotation, the env vars carried
-- Live price IDs but the table still held only Test rows -- staging
-- signups (Test) continued to work, prod signups (Live) hit
-- _lookup_subscription_plan, found nothing, and returned 404
-- "Subscription plan not found".
--
-- Surfaced 2026-05-23 during the §A prod shakeout (Task #38).
--
-- Lookups in routes/billing_routes.py are by stripe_price_id (or
-- stripe_product_id), never by name. So holding multiple rows per name --
-- one per Stripe mode -- is safe: the caller's Stripe key dictates which
-- price_id arrives, which deterministically selects the right row.

-- 1) Add the stripe_mode column. NOT NULL with a guarded default so the
--    backfill in step 2 can run idempotently on a table that already has
--    the column from a prior partial apply.
ALTER TABLE subscription_plans
  ADD COLUMN IF NOT EXISTS stripe_mode TEXT;

-- Backfill existing rows. Distinguish Test vs Live by Stripe key prefix:
-- Stripe price IDs are mode-opaque (both shaped `price_<24+ chars>`),
-- but the rows present in prod on 2026-05-23 were all Test rows seeded
-- from the prior Test key. After this migration, future seeders must
-- set stripe_mode explicitly.
UPDATE subscription_plans
SET stripe_mode = 'test'
WHERE stripe_mode IS NULL;

ALTER TABLE subscription_plans
  ALTER COLUMN stripe_mode SET NOT NULL;

-- Mode CHECK. ('test', 'live') matches Stripe's two API modes.
ALTER TABLE subscription_plans
  DROP CONSTRAINT IF EXISTS ck_subscription_plans_stripe_mode;
ALTER TABLE subscription_plans
  ADD CONSTRAINT ck_subscription_plans_stripe_mode
  CHECK (stripe_mode IN ('test', 'live'));

-- 2) Replace UNIQUE(name) with UNIQUE(name, stripe_mode). The old
--    constraint forced exactly one row per plan name across modes; we
--    now want exactly one per (plan name, mode) pair so 'starter' can
--    exist in both Test and Live.
ALTER TABLE subscription_plans
  DROP CONSTRAINT IF EXISTS uq_subscription_plans_name;
ALTER TABLE subscription_plans
  DROP CONSTRAINT IF EXISTS uq_subscription_plans_name_stripe_mode;
ALTER TABLE subscription_plans
  ADD CONSTRAINT uq_subscription_plans_name_stripe_mode
  UNIQUE (name, stripe_mode);

-- 3) Seed Live rows. Price IDs come from the prod Stripe Live key
--    rotation on 2026-05-22 -- same values as the prod web service env
--    vars STRIPE_PRICE_ID_{STARTER,GROWTH,SCALE}. Keep id BIGSERIAL
--    auto-assignment; the lookup is by stripe_price_id, not id.
--
--    ON CONFLICT keeps this migration idempotent if re-run.
INSERT INTO subscription_plans
  (name, stripe_price_id, price_cents, tier_level,
   monthly_credit_allowance, status, stripe_mode)
VALUES
  ('starter', 'price_1TZiKzKBoATcx2vH2N0Zpt6v',
   9900, 1, 1000, 'active', 'live'),
  ('growth',  'price_1TZiL0KBoATcx2vHYIcGh1wI',
   29900, 2, 5000, 'active', 'live'),
  ('scale',   'price_1TZiL1KBoATcx2vHkdc0AWxF',
   99900, 3, 25000, 'active', 'live')
ON CONFLICT (stripe_price_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_subscription_plans_stripe_mode
  ON subscription_plans(stripe_mode);

COMMENT ON COLUMN subscription_plans.stripe_mode IS
  'Stripe API mode this row belongs to. Test rows for staging, Live rows for prod. Lookups are by stripe_price_id (not by name), so the column is operator-facing only.';

-- DOWN (manual rollback only):
-- DELETE FROM subscription_plans WHERE stripe_mode = 'live';
-- ALTER TABLE subscription_plans DROP CONSTRAINT IF EXISTS uq_subscription_plans_name_stripe_mode;
-- ALTER TABLE subscription_plans ADD CONSTRAINT uq_subscription_plans_name UNIQUE (name);
-- ALTER TABLE subscription_plans DROP CONSTRAINT IF EXISTS ck_subscription_plans_stripe_mode;
-- DROP INDEX IF EXISTS idx_subscription_plans_stripe_mode;
-- ALTER TABLE subscription_plans DROP COLUMN IF EXISTS stripe_mode;

-- 126_subscription_plans_allowance_rebase.sql
-- True-up subscription_plans.monthly_credit_allowance to build brief §4
-- defaults: Starter 4,000 / Growth 18,000 / Scale 75,000 credits per month.
--
-- Prior values (1,000 / 5,000 / 25,000 from migrations 101 and 124) were
-- 4× short of the brief. The brief is the source of truth: the financial
-- model in /Users/pengchydan/dev/Markato_Channel_Policy_Model.xlsx prices
-- the tiers around the higher allowances, so leaving the lower numbers in
-- prod would silently sell each plan with a smaller bundle than the model
-- assumes.
--
-- Safety: per the 2026-05-23 prod recap, the v1.3 system on Live has zero
-- active paid subscribers (shakeout merchant id=20 has stripe_customer_id
-- NULL, never paid). Code path: monthly_credit_allowance is read at
-- subscription-start time (routes/billing_routes.py:319) and written into
-- merchant_credits.tier_allowance + an initial credit_ledger grant. There
-- is no monthly renewal-grant cron that re-reads it. So bumping the
-- catalogue value only affects NEW signups and re-subscriptions; no
-- existing rows need backfilling here.
--
-- PR #2 of the channel-partner program rebuild (sequence in
-- /Users/pengchydan/dev/PIVOTA_PROGRESS_2026_05_23.md). The companion
-- "dollar-leak audit" of merchant-facing surfaces (build brief §3) was
-- run separately and came back clean — no code changes paired with this
-- migration.

UPDATE subscription_plans
SET monthly_credit_allowance = 4000
WHERE name = 'starter'
  AND monthly_credit_allowance <> 4000;

UPDATE subscription_plans
SET monthly_credit_allowance = 18000
WHERE name = 'growth'
  AND monthly_credit_allowance <> 18000;

UPDATE subscription_plans
SET monthly_credit_allowance = 75000
WHERE name = 'scale'
  AND monthly_credit_allowance <> 75000;

-- The `monthly_credit_allowance <> X` guard keeps this idempotent: the
-- second apply finds no rows to update and is a no-op (the
-- set_updated_at() trigger won't fire either, so updated_at stays clean).

-- DOWN (manual rollback only; restores the pre-brief allowances):
-- UPDATE subscription_plans SET monthly_credit_allowance = 1000  WHERE name = 'starter';
-- UPDATE subscription_plans SET monthly_credit_allowance = 5000  WHERE name = 'growth';
-- UPDATE subscription_plans SET monthly_credit_allowance = 25000 WHERE name = 'scale';

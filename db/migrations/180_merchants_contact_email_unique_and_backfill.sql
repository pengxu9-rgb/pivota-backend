-- Migration 180: backfill missing `merchants` billing rows + enforce one
-- billing account per contact_email.
--
-- Context (see PR #1346): merchant identity is split across two tables —
--   merchant_onboarding : CRM/funnel record, written at signup/approval.
--   merchants           : monetization/account record (stripe_customer_id,
--                         current_tier, credits), keyed by contact_email
--                         (it has no merchant_id column).
-- No production code path promoted an approved merchant_onboarding row into a
-- merchants row, so a first-time subscriber's checkout 500'd
-- ("local merchants row missing") and orphaned a Stripe customer. #1346 fixes
-- the go-forward path (self-provision at checkout). This migration:
--   (1) backfills merchants rows for merchants already stuck in the gap, and
--   (2) adds a UNIQUE index on LOWER(contact_email) so the row can never be
--       duplicated and the provisioning INSERT is race-safe at the DB layer.
--
-- Number 180 chosen after checking origin/main (max 179). Both statements are
-- idempotent and safe to re-run. NOTE: the unique index creation will fail if
-- merchants already contains duplicate contact_emails — verified none exist in
-- production before shipping; the backfill itself can't introduce a duplicate
-- (DISTINCT ON + WHERE NOT EXISTS).

-- (1) Backfill: one merchants row per approved onboarding email that lacks one.
INSERT INTO merchants (
    business_name, legal_name, platform, store_url,
    contact_email, status, verification_status,
    current_tier, created_at, updated_at
)
SELECT DISTINCT ON (LOWER(mo.contact_email))
    mo.business_name,
    mo.business_name,
    'custom',
    mo.store_url,
    mo.contact_email,
    'active',
    'verified',
    'free',
    NOW(),
    NOW()
FROM merchant_onboarding mo
WHERE mo.status = 'approved'
  AND mo.contact_email IS NOT NULL
  AND btrim(mo.contact_email) <> ''
  AND NOT EXISTS (
      SELECT 1 FROM merchants m
      WHERE LOWER(m.contact_email) = LOWER(mo.contact_email)
  )
ORDER BY LOWER(mo.contact_email), mo.created_at ASC;

-- (2) One billing account per email. Partial index skips blank emails
-- (contact_email is NOT NULL but empty strings are possible in legacy rows).
CREATE UNIQUE INDEX IF NOT EXISTS uq_merchants_lower_contact_email
  ON merchants (LOWER(contact_email))
  WHERE contact_email IS NOT NULL AND btrim(contact_email) <> '';

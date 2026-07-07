-- 171_partner_invite_tokens_multiuse.sql
-- Make channel-partner invite links MULTI-USE: one link a partner can share
-- with many merchants, valid until it expires or is revoked (instead of the
-- original single-use consume). consume() no longer flips the token to
-- 'consumed'; it stays 'active' and increments use_count per distinct merchant
-- that redeems it. max_uses is an optional soft cap (NULL = unlimited).
--
-- Backward compatible: existing tokens already in 'consumed'/'revoked'/'expired'
-- stay terminal (not redeemable). Existing still-'active' tokens simply become
-- reusable from now on. consumed_at is repurposed as "first redeemed at";
-- consumed_by_merchant_id is left as-is (only ever set by the old single-use
-- path) and superseded by use_count + partner_attribution rows for "who joined".

ALTER TABLE partner_invite_tokens
  ADD COLUMN IF NOT EXISTS use_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS max_uses INTEGER;  -- NULL = unlimited

-- Dropped-then-added so re-apply under the AUTOCOMMIT migration runner stays
-- idempotent (matches the migration-125 pattern).
ALTER TABLE partner_invite_tokens
  DROP CONSTRAINT IF EXISTS ck_partner_invite_tokens_use_count_nonneg;
ALTER TABLE partner_invite_tokens
  ADD CONSTRAINT ck_partner_invite_tokens_use_count_nonneg
  CHECK (use_count >= 0);

ALTER TABLE partner_invite_tokens
  DROP CONSTRAINT IF EXISTS ck_partner_invite_tokens_max_uses_positive;
ALTER TABLE partner_invite_tokens
  ADD CONSTRAINT ck_partner_invite_tokens_max_uses_positive
  CHECK (max_uses IS NULL OR max_uses > 0);

COMMENT ON COLUMN partner_invite_tokens.use_count IS
  'Distinct merchants that have redeemed this multi-use link (migration 171).';
COMMENT ON COLUMN partner_invite_tokens.max_uses IS
  'Optional soft cap on redemptions; NULL = unlimited (migration 171).';

-- DOWN (manual rollback only; the runner is UP-only):
-- ALTER TABLE partner_invite_tokens
--   DROP CONSTRAINT IF EXISTS ck_partner_invite_tokens_max_uses_positive,
--   DROP CONSTRAINT IF EXISTS ck_partner_invite_tokens_use_count_nonneg,
--   DROP COLUMN IF EXISTS max_uses,
--   DROP COLUMN IF EXISTS use_count;

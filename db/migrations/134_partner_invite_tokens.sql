-- 134_partner_invite_tokens.sql
-- Single-use, DB-backed invite links for channel-partner merchant attribution.
-- Numbered 134 because main already contained 132_catalog_offer_suppression_writer_audit
-- and 133_catalog_source_domain when this work landed; renumbered from the original
-- 132 in the codex brief to keep the sequence collision-free.

CREATE TABLE IF NOT EXISTS partner_invite_tokens (
  id BIGSERIAL PRIMARY KEY
);

ALTER TABLE IF EXISTS partner_invite_tokens
  ADD COLUMN IF NOT EXISTS channel_partner_id BIGINT NOT NULL,
  ADD COLUMN IF NOT EXISTS token_hash TEXT NOT NULL,
  ADD COLUMN IF NOT EXISTS token_prefix TEXT NOT NULL,
  ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NOT NULL,
  ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active',
  ADD COLUMN IF NOT EXISTS issued_by TEXT,
  ADD COLUMN IF NOT EXISTS notes TEXT,
  ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS consumed_by_merchant_id VARCHAR(50),
  ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS revoked_by TEXT,
  ADD COLUMN IF NOT EXISTS revoked_reason TEXT,
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE IF EXISTS partner_invite_tokens
  DROP CONSTRAINT IF EXISTS partner_invite_tokens_channel_partner_id_fkey;
ALTER TABLE IF EXISTS partner_invite_tokens
  ADD CONSTRAINT partner_invite_tokens_channel_partner_id_fkey
  FOREIGN KEY (channel_partner_id) REFERENCES channel_partners(id) ON DELETE RESTRICT;

ALTER TABLE IF EXISTS partner_invite_tokens
  DROP CONSTRAINT IF EXISTS ck_partner_invite_tokens_status;
ALTER TABLE IF EXISTS partner_invite_tokens
  ADD CONSTRAINT ck_partner_invite_tokens_status
  CHECK (status IN ('active', 'consumed', 'revoked', 'expired'));

ALTER TABLE IF EXISTS partner_invite_tokens
  DROP CONSTRAINT IF EXISTS ck_partner_invite_tokens_token_hash_nonempty;
ALTER TABLE IF EXISTS partner_invite_tokens
  ADD CONSTRAINT ck_partner_invite_tokens_token_hash_nonempty
  CHECK (token_hash <> '');

ALTER TABLE IF EXISTS partner_invite_tokens
  DROP CONSTRAINT IF EXISTS ck_partner_invite_tokens_token_prefix_nonempty;
ALTER TABLE IF EXISTS partner_invite_tokens
  ADD CONSTRAINT ck_partner_invite_tokens_token_prefix_nonempty
  CHECK (token_prefix <> '');

ALTER TABLE IF EXISTS partner_invite_tokens
  DROP CONSTRAINT IF EXISTS ck_partner_invite_tokens_consumed_pair;
ALTER TABLE IF EXISTS partner_invite_tokens
  ADD CONSTRAINT ck_partner_invite_tokens_consumed_pair
  CHECK ((consumed_at IS NULL) = (consumed_by_merchant_id IS NULL));

ALTER TABLE IF EXISTS partner_invite_tokens
  DROP CONSTRAINT IF EXISTS ck_partner_invite_tokens_revoked_pair;
ALTER TABLE IF EXISTS partner_invite_tokens
  ADD CONSTRAINT ck_partner_invite_tokens_revoked_pair
  CHECK ((revoked_at IS NULL) = (revoked_by IS NULL));

CREATE UNIQUE INDEX IF NOT EXISTS ux_partner_invite_tokens_token_hash
  ON partner_invite_tokens (token_hash);
CREATE INDEX IF NOT EXISTS idx_partner_invite_tokens_partner_status
  ON partner_invite_tokens (channel_partner_id, status);
CREATE INDEX IF NOT EXISTS idx_partner_invite_tokens_expires_at
  ON partner_invite_tokens (expires_at) WHERE status = 'active';

DROP TRIGGER IF EXISTS trg_partner_invite_tokens_updated_at ON partner_invite_tokens;
CREATE TRIGGER trg_partner_invite_tokens_updated_at
  BEFORE UPDATE ON partner_invite_tokens
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

-- State machine: rows in terminal states cannot transition back to active or
-- any other status, and terminal attribution/audit fields become immutable.
CREATE OR REPLACE FUNCTION prevent_partner_invite_token_terminal_mutation()
RETURNS TRIGGER AS $$
BEGIN
  IF OLD.status IN ('consumed', 'revoked', 'expired') THEN
    IF NEW.status <> OLD.status THEN
      RAISE EXCEPTION 'partner_invite_tokens row % is in terminal status % and cannot transition', OLD.id, OLD.status;
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
      OR NEW.channel_partner_id IS DISTINCT FROM OLD.channel_partner_id
      OR NEW.token_hash IS DISTINCT FROM OLD.token_hash
      OR NEW.token_prefix IS DISTINCT FROM OLD.token_prefix
      OR NEW.consumed_at IS DISTINCT FROM OLD.consumed_at
      OR NEW.consumed_by_merchant_id IS DISTINCT FROM OLD.consumed_by_merchant_id
      OR NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
      OR NEW.revoked_by IS DISTINCT FROM OLD.revoked_by
      OR NEW.revoked_reason IS DISTINCT FROM OLD.revoked_reason
      OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
      RAISE EXCEPTION 'partner_invite_tokens row % does not allow mutation of terminal fields', OLD.id;
    END IF;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_partner_invite_tokens_terminal_guard ON partner_invite_tokens;
CREATE TRIGGER trg_partner_invite_tokens_terminal_guard
  BEFORE UPDATE ON partner_invite_tokens
  FOR EACH ROW EXECUTE PROCEDURE prevent_partner_invite_token_terminal_mutation();

COMMENT ON TABLE partner_invite_tokens IS
  'Partner-issued invite links for merchant attribution. Hashed at rest; raw token never persisted. Single-use single-status state machine.';

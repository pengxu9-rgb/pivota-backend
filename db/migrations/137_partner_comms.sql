-- 135_partner_comms.sql
-- Stage-1 partner comms layer: contact record + outbound send log.
-- Partners receive settlements by email from staff until the self-serve
-- portal (Surface 3) ships. This migration is idempotent.

CREATE TABLE IF NOT EXISTS partner_contacts (
  id BIGSERIAL PRIMARY KEY,
  channel_partner_id BIGINT NOT NULL UNIQUE
    REFERENCES channel_partners(id) ON DELETE RESTRICT,
  contact_name TEXT,
  contact_role TEXT,
  contact_email TEXT,
  cc_emails JSONB NOT NULL DEFAULT '[]'::jsonb,
  internal_notes TEXT,
  internal_notes_updated_by TEXT,
  internal_notes_updated_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_partner_contacts_updated_at ON partner_contacts;
CREATE TRIGGER trg_partner_contacts_updated_at
  BEFORE UPDATE ON partner_contacts
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

CREATE TABLE IF NOT EXISTS partner_send_log (
  id BIGSERIAL PRIMARY KEY,
  channel_partner_id BIGINT NOT NULL
    REFERENCES channel_partners(id) ON DELETE RESTRICT,
  settlement_file_id BIGINT
    REFERENCES settlement_files(id) ON DELETE SET NULL,
  template_id TEXT NOT NULL,
  to_email TEXT NOT NULL,
  cc_emails JSONB NOT NULL DEFAULT '[]'::jsonb,
  subject TEXT NOT NULL,
  body_text TEXT NOT NULL,
  sent_by TEXT NOT NULL,
  sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  provider_message_id TEXT,
  send_status TEXT NOT NULL DEFAULT 'sent',
  send_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_partner_send_log_status CHECK (send_status IN ('sent', 'failed')),
  CONSTRAINT ck_partner_send_log_template CHECK (template_id IN (
    'settlement_monthly',
    'settlement_skipped',
    'settlement_failed_notice'
  ))
);

CREATE INDEX IF NOT EXISTS idx_partner_send_log_partner
  ON partner_send_log (channel_partner_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_partner_send_log_settlement
  ON partner_send_log (settlement_file_id);

COMMENT ON TABLE partner_contacts IS
  'One contact record per channel partner — primary email, CC list, and internal notes for staff comms.';
COMMENT ON TABLE partner_send_log IS
  'Append-only log of outbound settlement emails sent to channel partners by Pivota staff.';

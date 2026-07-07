-- 172_partner_send_log_invite_template.sql
-- Allow the invite auto-email to be recorded in partner_send_log ("Recent
-- sends"). The original CHECK only permitted settlement templates, so an
-- invite send-log row was rejected. Widen it with 'partner_invite'.
--
-- Prod fast mode skips db/migrations/, so db/schema_guard.ensure_required_schema_light
-- performs the same DROP+ADD on startup — this file records the change for
-- non-prod / fresh DBs.

ALTER TABLE partner_send_log
  DROP CONSTRAINT IF EXISTS ck_partner_send_log_template;

ALTER TABLE partner_send_log
  ADD CONSTRAINT ck_partner_send_log_template CHECK (
    template_id IN (
      'settlement_monthly',
      'settlement_skipped',
      'settlement_failed_notice',
      'partner_invite'
    )
  );

-- DOWN (manual rollback only; the runner is UP-only):
-- ALTER TABLE partner_send_log DROP CONSTRAINT IF EXISTS ck_partner_send_log_template;
-- ALTER TABLE partner_send_log ADD CONSTRAINT ck_partner_send_log_template CHECK (
--   template_id IN ('settlement_monthly','settlement_skipped','settlement_failed_notice'));

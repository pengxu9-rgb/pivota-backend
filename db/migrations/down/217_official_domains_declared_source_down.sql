-- Down for 217: re-narrow the source CHECK to the three proven-or-derived values.
--
-- THIS FAILS WHILE ANY `declared` ROW EXISTS: Postgres validates the new CHECK
-- against existing rows. That is deliberate. Deleting merchant-supplied rows is
-- an operator decision, not something a rollback script should do silently;
-- run `SELECT merchant_id, domain FROM merchant_official_domains WHERE source =
-- 'declared'` first and decide what to do with them.
ALTER TABLE merchant_official_domains
  DROP CONSTRAINT IF EXISTS ck_merchant_official_domains_source;

ALTER TABLE merchant_official_domains
  ADD CONSTRAINT ck_merchant_official_domains_source
  CHECK (source IN ('asserted', 'verified', 'inferred'));

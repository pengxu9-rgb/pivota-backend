-- Down for 217: re-narrow the source CHECK to the three proven-or-derived values.
--
-- THIS FAILS WHILE ANY `declared` ROW EXISTS: Postgres validates the new CHECK
-- against existing rows. That is deliberate. Deleting merchant-supplied rows is
-- an operator decision, not something a rollback script should do silently;
-- run `SELECT merchant_id, domain FROM merchant_official_domains WHERE source =
-- 'declared'` first and decide what to do with them.
--
-- ROLL THE CODE BACK TOO. The model's CheckConstraint, the DDL backstop and
-- VALID_SOURCES in db/merchant_official_domains.py all still admit 'declared';
-- with only this file applied, insert_declared_domain fails the CHECK and the
-- route answers 500 write_failed. Down files here are documentation for an
-- operator, not something the runner applies.
ALTER TABLE merchant_official_domains
  DROP CONSTRAINT IF EXISTS ck_merchant_official_domains_source;

ALTER TABLE merchant_official_domains
  ADD CONSTRAINT ck_merchant_official_domains_source
  CHECK (source IN ('asserted', 'verified', 'inferred'));

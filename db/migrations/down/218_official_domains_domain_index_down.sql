-- Down for 218. NOTE: the DDL backstop in db/merchant_official_domains.py
-- re-creates this index (CREATE INDEX IF NOT EXISTS) on the next boot, so this
-- only sticks if the code is rolled back as well.
DROP INDEX IF EXISTS idx_merchant_official_domains_domain;

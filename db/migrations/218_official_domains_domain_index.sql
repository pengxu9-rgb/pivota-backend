-- 218: index merchant_official_domains on domain.
--
-- The cross-tenant proof lookup behind the declare endpoint
-- (PROVEN_BY_OTHER_SQL: WHERE domain = :domain AND merchant_id <> :merchant_id)
-- leads with `domain`, and no existing index does: the PK is (merchant_id,
-- domain) and the others lead with merchant_id or last_checked_at. Measured as
-- a Seq Scan removing 50,000 rows at 50,000 rows, twice per authenticated call.
-- Negligible at 42 merchants; cheap to fix before it is not.
--
-- Plain CREATE INDEX, not the online form: the table is tiny and this runs
-- inside the startup migration lock like every other index here.
CREATE INDEX IF NOT EXISTS idx_merchant_official_domains_domain
  ON merchant_official_domains (domain);

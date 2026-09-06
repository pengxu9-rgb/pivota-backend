-- 217: a merchant may DECLARE a domain, and a declaration is not a proof.
--
-- `merchant_official_domains.source` admitted exactly three values, and all
-- three assert evidence: `verified` means control proven AND bound to the
-- merchant's brand identity, `asserted` means control proven but unbound, and
-- `inferred` means Pivota derived it from the catalog. There was no way to
-- record "the merchant says this is theirs and nothing has been checked".
--
-- Without a fourth value the only options were to stamp an unproven host with
-- one of the proven sources — which makes it indistinguishable from a real one
-- in every later read — or to keep the set empty. It stayed empty: measured in
-- production 2026-09-06, ONE row across 42 merchants, with 16 of 17 audited
-- merchants falling back entirely to inference. That is the condition the
-- evidence base measured as a 13-point error on one brand's headline, because
-- inference knew anua.com and not anua.us.
--
-- `declared` is deliberately NOT in OFFICIAL_SOURCES (services/brand_claim_
-- service.py), so it does not widen the set that decides `first_party` on a
-- cited host. It exists so the portal can offer to verify it and so a brand
-- claim can be started against it; proving control promotes the row to
-- `verified` through the existing claim flow.
ALTER TABLE merchant_official_domains
  DROP CONSTRAINT IF EXISTS ck_merchant_official_domains_source;

ALTER TABLE merchant_official_domains
  ADD CONSTRAINT ck_merchant_official_domains_source
  CHECK (source IN ('asserted', 'verified', 'inferred', 'declared'));

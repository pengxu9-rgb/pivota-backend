-- 160_brand_claims.sql
-- P1: the brand CLAIM WRITE PATH — the one missing primitive. classify_offer_type
-- (services/offer_classification.py) only returns 'brand_direct' for an internal
-- merchant whose catalog_merchants.metadata_json.brand_relationship is the VERIFIED
-- value 'brand_direct' — but NO service wrote it (confirmed: zero writers). This
-- table records who claimed which brand/merchant, by what method, with what proof;
-- on successful verification the claim flow writes brand_relationship='brand_direct'
-- via services.brand_claim_service.set_merchant_brand_direct.
--
-- Storefront-agnostic by design: a brand need not own a store — claiming +
-- verification is domain/registry-based, and catalog_merchants is the
-- storefront-optional registry. This is the front door for store-less brands
-- (e.g. Anua/Anuko) to enter the index as a brand-attested canonical product.
--
-- Idempotent. Prod skips the migration runner — apply via railway ssh / admin.
BEGIN;

CREATE TABLE IF NOT EXISTS brand_claims (
    claim_id            UUID PRIMARY KEY,
    merchant_id         TEXT NOT NULL,          -- the catalog_merchants row being claimed
    brand_domain        TEXT NULL,              -- domain used for DNS/email verification
    content_key         VARCHAR(40) NULL,       -- optional: claim scoped to one entity
    claim_method        TEXT NOT NULL,          -- dns | email | amazon | shopify | manual
    verification_status TEXT NOT NULL DEFAULT 'pending',  -- pending | verified | rejected
    challenge_token     TEXT NULL,              -- the pivota-verify=<token> challenge value
    proof_ref           TEXT NULL,              -- where proof was found (dns:<host> / email / oauth sub)
    verified_at         TIMESTAMPTZ NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_user_id  TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_brand_claims_merchant
  ON brand_claims (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_brand_claims_status
  ON brand_claims (verification_status, claim_method);

COMMIT;

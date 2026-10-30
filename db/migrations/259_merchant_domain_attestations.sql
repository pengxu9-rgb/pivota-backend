-- 259: OPERATOR ATTESTATIONS that a domain is a brand's official store. Owner decision 2026-10-06.
--
-- WHY A SEPARATE TABLE. merchant_official_domains.source means "control was PROVEN" (verified =
-- proven and bound to the brand; asserted = proven, unbound; declared = unproven, untrusted). A
-- crawled brand store whose owner never engaged has no such proof, so writing it there would fake
-- the evidence that column exists to carry -- and OFFICIAL_SOURCES also decides first_party for
-- every cited host (AEO official share). An attestation is a different, weaker kind of evidence:
-- an operator reviewed independent sources and states the domain is the brand's own store.
--
-- Read ONLY by the enrichment lane's seller-type labelling (scripts/relabel_offer_seller_type.py),
-- as the `official_domain` input of services.offer_seller_identity.derive_offer_seller_identity.
-- Nothing else reads it; it never widens merchant_official_domains or citation tiers.
--
--   merchant_id   the catalog seller the offers are written under (agent_seed::<brand slug>).
--   domain        the attested host: lower case, no scheme/port/path, no leading www.
--   attested_by   who reviewed and approved (a person, never a job).
--   review_ref    where the approval lives (e.g. a dated review note / PR).
--   evidence      jsonb: the independent sources reviewed (urls, summary).
--   revoked_at    set to withdraw; a revoked row is never read. Rows are never deleted.
CREATE TABLE IF NOT EXISTS merchant_domain_attestations (
    merchant_id  TEXT NOT NULL,
    domain       TEXT NOT NULL,
    attested_by  TEXT NOT NULL,
    review_ref   TEXT NOT NULL,
    evidence     JSONB NOT NULL DEFAULT '{}'::jsonb,
    attested_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at   TIMESTAMPTZ NULL,
    PRIMARY KEY (merchant_id, domain),
    CONSTRAINT ck_merchant_domain_attestations_domain
      CHECK (
        domain = lower(domain)
        AND domain <> ''
        AND domain LIKE '%.%'
        AND domain NOT LIKE '% %'
        AND domain NOT LIKE '%/%'
        AND domain NOT LIKE '%:%'
        AND domain NOT LIKE '%.'
        AND domain NOT LIKE 'www.%'
      ),
    CONSTRAINT ck_merchant_domain_attestations_who
      CHECK (trim(attested_by) <> '' AND trim(review_ref) <> '')
);

CREATE INDEX IF NOT EXISTS idx_merchant_domain_attestations_domain
  ON merchant_domain_attestations (domain);

-- B1 — the verified, liveness-checked official-domain set.
--
-- WHAT THIS REPLACES. services/brand_claim_service.merchant_owned_domains()
-- INFERS a merchant's official hosts from merchant_onboarding.store_url /
-- website plus catalog_products.source_domain / canonical_url. That set decides
-- `first_party` on every cited host in the BD report, and measured on 840
-- grounded AI-shopping responses (2026-09-01) it was wrong in BOTH directions:
--
--   UNDERSTATED — anua.com and anua.us are byte-identical storefronts, but only
--   anua.com was ever inferred, so 7 citations of anua.us scored as retailer
--   traffic. Branded official share read 46% instead of 67%.
--
--   OVERSTATED — us.judydoll.com scored official and has NO DNS RECORD, as do
--   judydoll.shop, joocyee.co and judydoll-joygroup.com. A dead domain counted
--   as official inflates the headline number; a dead brand-token domain is a
--   hallucination the report should surface, not a destination.
--
-- So the set becomes ASSERTED or VERIFIED rather than inferred, MULTI-DOMAIN,
-- and LIVENESS-CHECKED. Inference is NOT removed — it stays the fallback tier
-- for merchants who have never asserted anything, and this table is where its
-- liveness verdict is recorded.
--
-- THE RULE THAT MAKES LIVENESS SAFE TO AUTOMATE, taken verbatim from
-- services/external_seed_destination_liveness.py: `unverifiable` is a
-- first-class outcome and it must NEVER buy an exclusion. 213 of 286 brand
-- hosts in that module's 2026-08-25 audit answered every request with a
-- Cloudflare challenge. Only a CONFIRMED NEGATIVE — NXDOMAIN, or a hard 404/410
-- on the apex after following redirects — sets liveness_status='dead', and only
-- 'dead' removes a domain from the official set.
--
-- Idempotent and safe to re-run.
--
-- Every statement below is deliberately portable to SQLite as well as Postgres
-- (CURRENT_TIMESTAMP not NOW(); LIKE predicates not btrim/regex), because the
-- inline DDL backstop in db/merchant_official_domains.py runs the SAME text and
-- the repo's hermetic tests run on SQLite. Do not "improve" one copy alone —
-- migrations + schema_guard are the schema truth and they must agree.
--
-- schema-guard-exempt: creates a new table only; adds no column to any existing
-- table, and db/merchant_official_domains.py carries the identical CREATE TABLE
-- as its own startup backstop.

CREATE TABLE IF NOT EXISTS merchant_official_domains (
    -- Association to the Pivota merchant whose official set this row belongs to.
    merchant_id         TEXT NOT NULL,
    -- Lower-cased registrable host: no scheme, path, port, trailing dot, or
    -- leading `www.`. Enforced by ck_merchant_official_domains_domain below,
    -- not merely by the Python normalizer — a caller that skips the normalizer
    -- must fail loudly rather than plant a host nothing will ever match.
    domain              TEXT NOT NULL,
    -- asserted = the merchant told us; verified = a brand claim proved control
    -- of it; inferred = derived from onboarding/catalog (the legacy tier).
    source              TEXT NOT NULL,
    -- brand_claims' vocabulary (db/brand_claims.py): pending/verified/failed.
    -- NULL for a row no claim lifecycle has touched.
    verification_status TEXT NULL,
    -- live / dead / unverifiable / unchecked. Only `dead` excludes a domain.
    liveness_status     TEXT NOT NULL DEFAULT 'unchecked',
    last_checked_at     TIMESTAMPTZ NULL,
    is_primary          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (merchant_id, domain),
    CONSTRAINT ck_merchant_official_domains_domain
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
    CONSTRAINT ck_merchant_official_domains_source
      CHECK (source IN ('asserted', 'verified', 'inferred')),
    CONSTRAINT ck_merchant_official_domains_verification
      CHECK (
        verification_status IS NULL
        OR verification_status IN ('pending', 'verified', 'failed')
      ),
    CONSTRAINT ck_merchant_official_domains_liveness
      CHECK (liveness_status IN ('live', 'dead', 'unverifiable', 'unchecked')),
    -- "We looked" must carry WHEN we looked. Without this a stale verdict is
    -- indistinguishable from a fresh one and the TTL sweep can never find it.
    CONSTRAINT ck_merchant_official_domains_checked_at
      CHECK (liveness_status = 'unchecked' OR last_checked_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_merchant_official_domains_merchant
  ON merchant_official_domains (merchant_id, source);

-- The TTL sweep's access path: stalest first, NULL (never checked) first of all.
CREATE INDEX IF NOT EXISTS idx_merchant_official_domains_liveness_due
  ON merchant_official_domains (last_checked_at);

COMMENT ON TABLE merchant_official_domains IS
  'B1: a merchant''s official storefront domains, asserted/verified rather than inferred, multi-domain, and liveness-checked. Consumed by services/brand_claim_service.merchant_owned_domains, which feeds build_authority_map(merchant_extra_hosts=...).';
COMMENT ON COLUMN merchant_official_domains.domain IS
  'Lower-cased registrable host, no scheme/path/port/trailing dot/leading www. The CHECK constraint is the enforcement; the Python normalizer is the convenience.';
COMMENT ON COLUMN merchant_official_domains.source IS
  'asserted (merchant told us) | verified (a brand claim proved domain control) | inferred (derived from onboarding/catalog — the pre-B1 tier, kept as the fallback).';
COMMENT ON COLUMN merchant_official_domains.liveness_status IS
  'live | dead | unverifiable | unchecked. ONLY dead excludes the domain from the official set. unverifiable (Cloudflare challenge, 429, timeout, TLS error) is a first-class outcome and must never buy an exclusion — see services/external_seed_destination_liveness.py.';
COMMENT ON COLUMN merchant_official_domains.is_primary IS
  'The domain to prefer when one must be chosen (e.g. anua.com over anua.us). Advisory only; it does not affect membership of the official set.';

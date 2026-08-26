-- 203: payment_grant_issuers — which PSPs may authorize MONEY, promoted out of an env var.
--
-- WHY THIS EXISTS. `complete_checkout` is the only canonical operation with
-- requiresPaymentAuthz: its inline payment_data grant is verified by the gateway's
-- createSignedGrantVerifier against PAYMENT_ISSUERS_JSON — an env var that holds exactly one
-- entry (the platform canary) and needs a gateway redeploy per partner. The identity half of
-- this problem was already solved properly (agent_identity_issuers, migration 193): a DB
-- registry, a portal, an internal endpoint the gateway pulls with a TTL cache and bounded
-- staleness. This table is that same shape for payment issuers, so onboarding Antom is a row,
-- not a rollout.
--
-- THE DELIBERATE DIVERGENCE FROM 193, stated where the schema lives: identity issuers are
-- AGENT self-service — an agent vouching for who its own buyer is. A payment issuer's grant
-- MOVES MONEY (create_order + mint_confirmation + submit_payment), so trusting one is
-- Pivota's decision: rows here are written by admin/employee only, and there is NO agent_id —
-- the trust is platform-global, scoped instead by `methods` (what kind of authorization the
-- issuer may mint) and pinned `audience`/`algs`/`authorized_party` exactly like the gateway
-- verifier's config entries, because these rows BECOME those entries.
--
--   issuer            exact `iss` of the grant JWT / AP2 mandate.
--   jwks_uri          pinned https JWKS (dereferenced at registration; jku/x5u never honoured).
--   audience          required `aud` of grants this issuer mints for Pivota.
--   algs              asymmetric allowlist (RS*/PS*/ES*/EdDSA). Never HS*, never none.
--   authorized_party  optional required `azp` claim.
--   methods           what the issuer is trusted to authorize: signed_grant and/or ap2_mandate.
--                     ap2_mandate rows are inert until the gateway's reviewed checkout-hash
--                     verifier ships — registering trust ahead of the verifier is fine;
--                     ENFORCING with it is what the gateway flag still refuses.
--   expected_vct      optional verifiable-credential type the AP2 verifier pins.
--   status            active | disabled. Disabled rows kept for audit; unique index ignores
--                     them so an issuer can be re-registered.

CREATE TABLE IF NOT EXISTS payment_grant_issuers (
    id               BIGSERIAL PRIMARY KEY,
    issuer           TEXT        NOT NULL,
    jwks_uri         TEXT        NOT NULL,
    audience         TEXT        NOT NULL,
    algs             TEXT[]      NOT NULL DEFAULT ARRAY['RS256', 'ES256']::TEXT[],
    authorized_party TEXT        NULL,
    methods          TEXT[]      NOT NULL DEFAULT ARRAY['signed_grant']::TEXT[],
    expected_vct     TEXT        NULL,
    status           TEXT        NOT NULL DEFAULT 'active',
    registered_by    TEXT        NOT NULL,
    last_jwks_ok_at  TIMESTAMPTZ NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT payment_grant_issuers_status_check CHECK (status IN ('active', 'disabled')),
    CONSTRAINT payment_grant_issuers_jwks_https CHECK (jwks_uri LIKE 'https://%'),
    CONSTRAINT payment_grant_issuers_methods_check CHECK (
        methods <@ ARRAY['signed_grant', 'ap2_mandate']::TEXT[]
        AND array_length(methods, 1) >= 1
    )
);

-- One ACTIVE row per issuer string: the verifier registry is keyed by `iss`, so trust must be
-- a function of the issuer.
CREATE UNIQUE INDEX IF NOT EXISTS payment_grant_issuers_active_issuer_uidx
    ON payment_grant_issuers (issuer)
    WHERE status = 'active';

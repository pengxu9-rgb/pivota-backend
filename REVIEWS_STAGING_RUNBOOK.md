# Reviews Center — Staging Runbook (Buyer Submission + Proof Issuer)

This runbook covers the staging verification for:
- multi-agent entry layer (discovery + resolve)
- buyer review submission (pending → employee approve → read path visible)
- signed media read (`/agent/shop/v1/review-media/{public_id}?exp&sig`)
- proof issuer service (multi-platform-ready) + exchange replay protection

Security note:
- Do not expose `X-Internal-Key` to browsers. The proof issuer must be called server-side only.
- Do not paste tokens/secrets into logs or tickets; use placeholders like `<REDACTED>`.

## Services

- **Reviews backend**: `https://<REVIEWS_BASE_URL>`
  - Read: `POST /agent/shop/v1/invoke`
  - Media: `GET /agent/shop/v1/review-media/{public_id}?exp&sig`
  - Buyer submit: `/buyer/reviews/v1/*`
  - Employee moderation: `/employee/reviews/v1/*`

- **Proof issuer**: `https://<PROOF_ISSUER_BASE_URL>`
  - `POST /internal/reviews/v1/proof/issue` (requires `X-Internal-Key`)
  - `GET /health`, `GET /__build`

## Required Railway env vars

### Reviews backend
- `REVIEWS_BUYER_PROOF_SIGNING_SECRET`: validates proof tokens from the issuer
- (optional, to accept `invitation_token` at `/buyer/reviews/v1/verification/exchange`): `REVIEWS_PROOF_ISSUER_BASE_URL`, `REVIEWS_PROOF_ISSUER_INTERNAL_KEY` (or reuse `REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY`)
- `REVIEWS_BUYER_SUBMIT_MERCHANT_ALLOWLIST`: comma-separated merchant ids for canary (empty = allow all)
- `REVIEWS_MEDIA_SIGNING_SECRET`: signs `/review-media` URLs
- `JWT_SECRET_KEY`: employee JWT signing secret (used by smoke scripts)

### Proof issuer
- `REVIEWS_BUYER_PROOF_SIGNING_SECRET`: must match the reviews backend secret
- `REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY`: internal auth for minting proofs
- (optional): `REVIEWS_BUYER_INVITATION_SIGNING_SECRET` (required if issuing `invitation_token`)
- `REVIEWS_BUYER_SUBMIT_MERCHANT_ALLOWLIST`: (optional) canary allowlist, recommended to match backend

## One-shot validation

Run from repo root:

```bash
REVIEWS_BASE_URL="https://pivota-backend-production.up.railway.app" \
PROOF_ISSUER_BASE_URL="https://proof-issuer-production.up.railway.app" \
MERCHANT_ID="<merchant_id>" \
PLATFORM="shopify" \
PLATFORM_PRODUCT_ID="<product_id>" \
VARIANT_ID="<variant_id_or_empty>" \
/bin/bash scripts/run_reviews_staging_checklist.sh
```

Or run from `pivota-backend/`:

```bash
cd pivota-backend
REVIEWS_BASE_URL="https://..." PROOF_ISSUER_BASE_URL="https://..." MERCHANT_ID="..." PLATFORM_PRODUCT_ID="..." ./scripts/run_reviews_staging_checklist.sh
```

The checklist covers:
- proof issuer `/health`
- entry layer `list_review_entrypoints` + `resolve_review_intent(write)`
- issuer→exchange replay protection
- buyer E2E: proof→exchange→create→upload→approve→visible→signed media 200/304 + signature negative tests

Optional invitation flow:

```bash
RUN_INVITATION_FLOW=true \
REVIEWS_BASE_URL="https://pivota-backend-production.up.railway.app" \
PROOF_ISSUER_BASE_URL="https://proof-issuer-production.up.railway.app" \
MERCHANT_ID="<merchant_id>" \
PLATFORM="shopify" \
PLATFORM_PRODUCT_ID="<product_id>" \
VARIANT_ID="<variant_id_or_empty>" \
/bin/bash scripts/run_reviews_staging_checklist.sh
```

## Proof issuer deployment (Railway)

Deploy proof issuer as its own Railway service using the `pivota-backend` repo/branch:
- **Start command**: `uvicorn proof_issuer_main:app --host 0.0.0.0 --port $PORT`
- **Healthcheck path**: `/health`

## Rollout guidance (canary → prod)

1. Keep buyer write entrypoint gated by `REVIEWS_BUYER_SUBMIT_MERCHANT_ALLOWLIST`.
2. Start with 1–3 merchants and observe:
   - submission success/error
   - under_review → active approval latency
   - media upload/read success rate
3. Expand allowlist gradually; rollback is immediate by removing merchants from allowlist.

See `REVIEWS_CANARY_PROD_CHECKLIST.md` for the full canary→prod checklist and the gating matrix checks.

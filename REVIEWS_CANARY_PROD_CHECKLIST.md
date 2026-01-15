# Reviews Center — Canary → Prod Checklist (Buyer Submission + Proof Issuer)

This checklist assumes:
- **Reviews backend** serves read-only invoke + signed media, and hosts buyer submission endpoints.
- **Proof issuer** is an internal-only service that validates a purchase proof (platform/order) and mints a short-lived **proof_token**.
- The reviews backend exchanges `proof_token` → `submission_token` (`/buyer/reviews/v1/verification/exchange`) with replay protection.

Security note:
- `X-Internal-Key` must never reach browsers; call proof issuer from server-side only (e.g. API route / backend).
- Do not log tokens/secrets; use `<REDACTED>` in tickets/examples.

## Stage 0 — Prereqs (P0)

- Confirm both services build:
  - Reviews backend: `GET /__build`
  - Proof issuer: `GET /__build`, `GET /health`
- Confirm secrets are set:
  - Both: `REVIEWS_BUYER_PROOF_SIGNING_SECRET` (must match)
  - Proof issuer only: `REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY`
  - Proof issuer only (optional, for invitation tokens): `REVIEWS_BUYER_INVITATION_SIGNING_SECRET`
  - Reviews backend: `REVIEWS_MEDIA_SIGNING_SECRET`, `JWT_SECRET_KEY`
- If you want the reviews backend to accept `invitation_token` at `/buyer/reviews/v1/verification/exchange`:
  - Reviews backend: `REVIEWS_PROOF_ISSUER_BASE_URL`, `REVIEWS_PROOF_ISSUER_INTERNAL_KEY` (or reuse `REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY`)
- Confirm canary gating is configured on reviews backend:
  - `REVIEWS_BUYER_SUBMIT_ENABLED=true`
  - `REVIEWS_BUYER_SUBMIT_MERCHANT_ALLOWLIST=<comma-separated merchant ids>`

## Stage 1 — Canary smoke (P0)

Run:

```bash
cd pivota-backend
REVIEWS_BASE_URL="https://<reviews-host>" \
PROOF_ISSUER_BASE_URL="https://<proof-issuer-host>" \
MERCHANT_ID="<canary_merchant_id>" \
PLATFORM="shopify" \
PLATFORM_PRODUCT_ID="<product_id>" \
VARIANT_ID="<variant_id_or_empty>" \
./scripts/run_reviews_staging_checklist.sh
```

This verifies:
- entry layer discovery/resolve (`list_review_entrypoints`, `resolve_review_intent(write)`)
- proof issuer → exchange replay protection (2nd exchange returns 409)
- buyer write end-to-end: `under_review → employee approve → active visible via read path`
- signed media: `200 + ETag`, `304`, missing sig → `403`, tamper exp → `403`

Optional (invitation token path):

```bash
cd pivota-backend
PROOF_ISSUER_BASE_URL="https://<proof-issuer-host>" \
REVIEWS_BASE_URL="https://<reviews-host>" \
MERCHANT_ID="<canary_merchant_id>" \
PLATFORM="shopify" \
PLATFORM_PRODUCT_ID="<product_id>" \
VARIANT_ID="<variant_id_or_empty>" \
./scripts/smoke_buyer_review_via_invitation.sh
```

## Stage 2 — Canary gating checks (P0)

### 2.1 Write entrypoint allowed only for allowlisted merchants

Run for allowlisted merchant (expect allowed):

```bash
BASE_URL="https://<reviews-host>" MERCHANT_ID="<canary_merchant_id>" PLATFORM="shopify" PLATFORM_PRODUCT_ID="<product_id>" ./scripts/verify_reviews_buyer_canary.sh
```

Run for non-allowlisted merchant (expect denied):

```bash
BASE_URL="https://<reviews-host>" MERCHANT_ID="merch_not_allowed" PLATFORM="shopify" PLATFORM_PRODUCT_ID="<product_id>" EXPECT_WRITE_ALLOWED=false ./scripts/verify_reviews_buyer_canary.sh
```

### 2.2 Internal issuer disabled (recommended)

Keep `/buyer/reviews/v1/verification/issue-token` disabled in production:
- Ensure `REVIEWS_BUYER_SUBMIT_INTERNAL_ISSUER_ENABLED=false` on the reviews backend.

## Stage 3 — Observability (P1)

Confirm these exist on the reviews backend `/metrics` (auth required in your setup):
- `reviews_invoke_requests_total` / `reviews_invoke_errors_total`
- `reviews_media_sig_verify_failed_total` (missing/expired/bad_signature)
- `reviews_media_rate_limited_total`
- buyer submit:
  - `reviews_buyer_exchange_total`
  - `reviews_buyer_create_total`
  - `reviews_buyer_media_upload_total`

Add alerts (starter thresholds):
- spike in `reviews_media_sig_verify_failed_total{reason="bad_signature"}` (possible abuse)
- elevated 5xx on `/buyer/reviews/v1/*` and `/employee/reviews/v1/reviews/{id}/status`

## Stage 3.1 — Storage hygiene (P1)

Because canary deployments often change storage backends, it’s easy to accumulate stale rows.
Run a periodic cleanup to remove expired replay JTIs and old idempotency keys:

```bash
DATABASE_URL="<public postgres url>" ./scripts/cleanup_buyer_submission_tables.py
DATABASE_URL="<public postgres url>" ./scripts/cleanup_buyer_submission_tables.py --apply
```

## Stage 4 — Rollout to prod (P0)

1. Deploy both services to prod with `REVIEWS_BUYER_SUBMIT_ENABLED=false` (dark launch).
2. Enable canary:
   - set `REVIEWS_BUYER_SUBMIT_ENABLED=true`
   - set `REVIEWS_BUYER_SUBMIT_MERCHANT_ALLOWLIST=<small set>`
3. Expand allowlist gradually.
4. Rollback is immediate:
   - remove merchant from allowlist (or set `REVIEWS_BUYER_SUBMIT_ENABLED=false`)

## Multi-platform note

The proof issuer is where “platform-specific verification” lives.
It should validate a platform/order proof and mint a `proof_token` that contains only:
- `merchant_id`
- `subjects[]` with `{platform, platform_product_id, variant_id?}`
- `exp`, `jti`, `verification`

No PII should be included in the proof token.

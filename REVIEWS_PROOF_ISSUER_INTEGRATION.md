# Reviews Proof Issuer — Multi-platform Integration Notes

The Reviews Proof Issuer is the “platform/order verification” component. Its job is to:
1) validate a buyer’s purchase proof for a given merchant + product/variant, and
2) mint a short-lived **proof_token** that the Reviews backend can exchange for a **submission_token**.

This keeps platform-specific logic out of the Reviews backend and avoids any buyer PII in the Reviews DB.

## Why a proof issuer exists

- The Reviews backend should not:
  - store buyer identifiers,
  - depend on Shopify/Amazon/Wix APIs,
  - embed platform-specific verification logic in `/buyer/reviews/v1/*`.
- The proof issuer is where you can safely plug in:
  - order verification,
  - anti-abuse heuristics tied to platform events,
  - platform expansion without touching the Reviews read path.

## Contracts

### 1) Proof issuer → mint proof_token (internal-only)

Endpoint:
- `POST /internal/reviews/v1/proof/issue`

Auth:
- `X-Internal-Key: <REDACTED>` (server-side only)

Request body:
- `merchant_id`
- `subjects[]` with:
  - `platform`
  - `platform_product_id`
  - optional `variant_id`
- `verification` (`unverified` or `verified_buyer`)
- `ttl_seconds`

Response:
- `proof_token` (opaque, short-lived)
- `expires_at`

### 2) Reviews backend → exchange proof_token for submission_token

Endpoint:
- `POST /buyer/reviews/v1/verification/exchange`

Auth:
- `Authorization: Bearer <proof_token>`

Response:
- `submission_token` (opaque, short-lived)
- `expires_at`

Replay:
- exchanging the same `proof_token` twice must return `409 REPLAY_DETECTED`.

## Optional: post-purchase invitation tokens (recommended for browser flows)

If you need a browser-safe link/token (e.g. order confirmation page) without leaking `X-Internal-Key`,
use an `invitation_token`:

1) An upstream server (connector/order service) calls the proof issuer (internal-only) to mint an invitation:
   - `POST /internal/reviews/v1/invitation/issue` (`X-Internal-Key` required)
   - returns `invitation_token`
2) The browser/client submits that `invitation_token` to the reviews backend exchange endpoint:
   - `POST /buyer/reviews/v1/verification/exchange`
   - `Authorization: Bearer <invitation_token>`
3) The reviews backend calls the proof issuer internally to exchange invitation → proof, then proceeds as usual.

Notes:
- Proof issuer must have `REVIEWS_BUYER_INVITATION_SIGNING_SECRET` set.
- Reviews backend must be configured with:
  - `REVIEWS_PROOF_ISSUER_BASE_URL`
  - `REVIEWS_PROOF_ISSUER_INTERNAL_KEY` (or reuse `REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY`)
- Replay is enforced by reusing the same `jti` between invitation and proof tokens (2nd exchange returns 409).

### Shortcut for Pivota orders (server-side)

If your order service runs in the same `pivota-backend` monolith and has access to the Orders DB,
you can mint an invitation from a *paid order* without touching buyer PII:
- `POST /internal/reviews/v1/invitation/issue-from-order` (requires `X-Internal-Key`)
- This endpoint:
  - validates the order is paid
  - extracts `subjects[]` from `orders.items[]` (`product_id` + optional `variant_id`)
  - calls the proof issuer `/internal/reviews/v1/invitation/issue` internally

Smoke:
- `./scripts/smoke_issue_review_invitation_from_order.sh`

## Multi-platform rules

The only platform-specific field that flows into Reviews is:
- `platform` (string)
- `platform_product_id` (string)
- optional `variant_id` (string)

Everything else stays on the issuer side.

## Where to put issuer logic

In production, the issuer should live in a dedicated “platform/order verification service” (or inside your existing connector service).

Recommended layering:
- **platform connector** (Shopify/Amazon/…):
  - validates purchase proof
  - resolves which `subjects[]` the buyer is allowed to review
  - calls the proof issuer mint endpoint (or directly performs minting if co-located)
- **reviews backend**:
  - exchanges proof → submission_token
  - accepts buyer write using submission_token (pending → employee moderation)
  - never calls platform APIs

## Smoke tests

From `pivota-backend/`:

- `./scripts/smoke_reviews_proof_issuer_exchange.sh`
- `./scripts/smoke_buyer_review_via_proof_issuer.sh`
- `./scripts/run_reviews_staging_checklist.sh`

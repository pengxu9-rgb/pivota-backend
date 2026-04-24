# Reviews Invitation Emails (SendGrid) — Runbook

This runbook covers sending buyer review invitation emails after delivery/shipping, using the existing Reviews Buyer Submission + Proof Issuer flow.

## Components

- **Reviews backend (prod):** `https://api.pivota.cc`
  - Mints `invitation_token` from paid orders (server-side only)
  - Sends email via SendGrid (server-side only)
- **Proof issuer (prod):** `https://reviews-proof-issuer-production.up.railway.app`
  - Mints `invitation_token` / `proof_token` (server-side only)
- **Buyer UI landing page (future):** `https://agent.pivota.cc/reviews/write`
  - The invitation token should be passed via **URL fragment** (not query) to reduce Referer leakage:
    - `https://agent.pivota.cc/reviews/write#invitation_token=<token>`

## Required env (Reviews backend: `api.pivota.cc`)

**Invitation link**
- `REVIEWS_BUYER_INVITATION_LINK_BASE_URL=https://agent.pivota.cc/reviews/write`

**Internal auth (issuer)**
- Recommended: set a dedicated key for this issuer endpoint:
  - `REVIEWS_INVITATION_ISSUER_INTERNAL_KEY=<REDACTED>`
- If not set, the code falls back to:
  - `REVIEWS_PROOF_ISSUER_INTERNAL_KEY` or `REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY`

**Proof issuer connectivity**
- `REVIEWS_PROOF_ISSUER_BASE_URL=https://reviews-proof-issuer-production.up.railway.app`
- `REVIEWS_PROOF_ISSUER_INTERNAL_KEY=<REDACTED>` (must match proof-issuer service)

**SendGrid**
- `SENDGRID_API_KEY=<REDACTED>`
- `FROM_EMAIL=<configured sender>`
- Optional (recommended): `REVIEWS_INVITATION_SENDGRID_TEMPLATE_ID=<REDACTED>`
  - If not set, the service sends a plain-text email with the invitation links.

## Internal endpoints (server-side only)

**Mint invitation tokens/links from a paid order**
- `POST /internal/reviews/v1/invitation/issue-from-order`
- Requires header: `X-Internal-Key: <REDACTED>`

**Send invitation email from a paid order (mints token internally)**
- `POST /internal/reviews/v1/invitation/send-email-from-order`
- Requires header: `X-Internal-Key: <REDACTED>`

## Manual smoke (one order)

From repo root (`pivota-backend-clean`):

- Send email (does not print buyer email nor tokens):
  - `REVIEWS_BASE_URL="https://api.pivota.cc" MERCHANT_ID="<...>" ORDER_ID="<...>" /bin/bash scripts/smoke_send_reviews_invitation_email_from_order.sh`

## Automated sending (Railway loop worker)

Railway doesn’t provide cron, so we run a small loop service.

**Start command**
- `/bin/bash scripts/run_reviews_invitation_email_loop.sh`

**Worker env**
- `DATABASE_URL=<prod postgres url>`
- `REVIEWS_BASE_URL=https://api.pivota.cc`
- `REVIEWS_INVITATION_ISSUER_INTERNAL_KEY=<same as backend>`
- Optional tuning:
  - `SLEEP_SECONDS=3600`
  - `DELIVERED_AFTER_DAYS=3`
  - `SHIPPED_AFTER_DAYS=10`
  - `PAID_AFTER_DAYS=14`
  - `TTL_SECONDS=604800`
  - `MAX_LINKS=3`
  - `LIMIT=50`

## Rollout strategy

1) Keep buyer submit gated (canary allowlist) using:
   - `REVIEWS_BUYER_SUBMIT_MERCHANT_ALLOWLIST=<comma-separated merchant_ids>`
2) Run the loop worker with a small `LIMIT` and longer `SLEEP_SECONDS` initially.
3) Monitor metrics:
   - `reviews_buyer_exchange_total`
   - `reviews_buyer_create_total`
   - `reviews_buyer_media_upload_total`
4) Expand allowlist gradually; clear allowlist only when ready for broader exposure.

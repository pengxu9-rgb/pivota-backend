# Reviews Invitation (Order → Email) Runbook

## What it is
- When an order transitions to `status=completed` and/or `fulfillment_status in (shipped, delivered)`, the backend enqueues a row into `reviews_invitation_send_jobs`.
- A worker service periodically runs `scripts/process_due_reviews_invitation_send_jobs.py` and calls the internal endpoint `POST /internal/reviews/v1/invitation/send-email-from-order` to send the email via SendGrid.
- The email uses a short link (`/r/{code}`) that redirects (302) to the buyer write page with the token in the URL fragment.

## Required services
- Web backend (production): `https://api.pivota.cc`
- Proof issuer (production): `https://reviews-proof-issuer-production.up.railway.app`
- Invitation worker (production): runs `scripts/run_reviews_invitation_send_loop.sh`

## Environment variables (web backend)
- `REVIEWS_INVITATION_ISSUER_INTERNAL_KEY` (required for internal endpoints)
- `REVIEWS_PROOF_ISSUER_BASE_URL` (required)
- `REVIEWS_PROOF_ISSUER_INTERNAL_KEY` (required)
- `REVIEWS_BUYER_INVITATION_LINK_BASE_URL` (required, e.g. `https://agent.pivota.cc/reviews/write`)
- `REVIEWS_BUYER_INVITATION_SHORTLINK_BASE_URL` (recommended, e.g. `https://agent.pivota.cc/r`)
- `REVIEWS_INVITATION_SEND_DELAY_SECONDS` (recommended `>0`; if `0`, requires worker enabled)
- `REVIEWS_INVITATION_WORKER_ENABLED=true` (recommended; enables “send ASAP via worker”)
- SendGrid:
  - `SENDGRID_API_KEY`
  - `FROM_EMAIL` (sender, Pivota-owned)
  - Optional:
    - `REVIEWS_INVITATION_FROM_NAME`
    - `REVIEWS_INVITATION_DISABLE_SENDGRID_CLICK_TRACKING=true` (recommended to avoid long visible URLs)
    - `REVIEWS_INVITATION_SENDGRID_TEMPLATE_ID` + `REVIEWS_INVITATION_USE_SENDGRID_TEMPLATE=true`
- Buyer submit gating:
  - `REVIEWS_BUYER_SUBMIT_ENABLED=true`
  - `REVIEWS_BUYER_SUBMIT_MERCHANT_ALLOWLIST` (empty = allow all)

## Environment variables (invitation worker)
- `DATABASE_URL` (same Postgres as web backend)
- `REVIEWS_BASE_URL=https://api.pivota.cc` (recommended; avoids env drift)
- `REVIEWS_INVITATION_ISSUER_INTERNAL_KEY` (same as web backend)
- `REVIEWS_INVITATION_WORKER_ENABLED=true` and/or `REVIEWS_INVITATION_SEND_DELAY_SECONDS>0`
- `SLEEP_SECONDS` (default `60`)
- `REVIEWS_INVITATION_JOB_BATCH_SIZE` (default `10`)
- `REVIEWS_INVITATION_JOB_MAX_ATTEMPTS` (default `5`)
- `REVIEWS_INVITATION_JOB_STALE_SECONDS` (default `1800`)

## Verification (no SQL)
- Health/build:
  - `curl --http1.1 -sS https://api.pivota.cc/__build`
  - `curl --http1.1 -sS https://reviews-proof-issuer-production.up.railway.app/__build`
- Enqueue + send (requires internal key):
  - `POST /internal/reviews/v1/invitation/issue-from-order`
  - `POST /internal/reviews/v1/invitation/send-email-from-order`
  - `POST /internal/reviews/v1/invitation/enqueue-send-job`
  - `GET  /internal/reviews/v1/invitation/jobs/stats`

## Rollback
- Disable buyer submit entirely:
  - set `REVIEWS_BUYER_SUBMIT_ENABLED=false` (write entrypoints return disabled; worker will cancel jobs).
- Disable invitation job enqueue/sends:
  - set `REVIEWS_INVITATION_WORKER_ENABLED=false` and `REVIEWS_INVITATION_SEND_DELAY_SECONDS=0`
- Disable invitation issuer endpoints:
  - unset `REVIEWS_INVITATION_ISSUER_INTERNAL_KEY`


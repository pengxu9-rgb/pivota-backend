# Shopify App Review Submission (App A — public "Pivota" listing)

> Paste-ready reviewer-facing text. Every scope, webhook, and behavior below is
> in lockstep with the shipped config: `shopify.app.toml` (`[access_scopes]` +
> `[[webhooks.subscriptions]]`) and `config/settings.py:shopify_appstore_scopes`.
> Internal readiness detail + verification evidence:
> `PIVOTA-Agent/docs/shopify_app_review_submission_checklist_2026-07-14.md`.
> Do NOT reintroduce `write_orders`/`write_webhooks` here — those belong only to
> the custom/headless app (App B, never submitted for public review).

## App Overview
- App name: Pivota
- App URL: https://api.pivota.cc/integrations/shopify/app
- OAuth redirect URL:
  - https://api.pivota.cc/integrations/shopify/oauth/callback
- Install flow: managed install (`use_legacy_install_flow=false`). Shopify grants
  the scopes declared in the app config at install time; the app authenticates
  immediately after approval.
- Webhook API version: 2025-10 (pinned in `shopify.app.toml` to match the version
  the backend registers/calls).

## Installation Instructions
1) Provide your Shopify MyShopify domain (e.g. `your-store.myshopify.com`).
2) Open the one-time install link we provide.
3) Approve the requested (read-only) scopes.
4) Install completes and the app authenticates. Order and catalog sync begins;
   the app-owned webhook subscriptions (below) are managed by Shopify — the app
   does not request `write_webhooks` and does not self-register per-merchant
   webhooks.

## Test Store / Reviewer Access
- Store domain (MyShopify): [FILL_ME_IN]
- Admin access (collaborator or staff account): [FILL_ME_IN]
- Notes: [FILL_ME_IN]

## Core Functionality (What to Verify)
Pivota is a read-only merchant tool: it syncs catalog and order data and provides
AI-readiness/optimization insights. It never writes to the store.
- Install & authenticate: after approving the read-only scopes the app lands on a
  success screen and holds a working access token (managed install auto-grants
  the declared scopes).
- Order webhook delivery: place and pay for a test order → Shopify delivers
  `orders/paid` to `https://api.pivota.cc/webhooks/shopify/orders`. (Signature is
  HMAC-verified; unsigned requests are rejected with 401.)
- Catalog read: the app reads products (`read_products`) to provide product
  context and AI-readiness analysis.
- Uninstall: uninstall the app → Shopify delivers `app/uninstalled` to the same
  endpoint; Pivota marks the store disconnected and removes stored tokens.

## Webhook Subscriptions (app-owned, declared in the app config)
Delivered to `https://api.pivota.cc/webhooks/shopify/orders`:
- orders/create
- orders/paid
- orders/cancelled
- refunds/create
- app/uninstalled

GDPR/compliance topics delivered to `https://api.pivota.cc/webhooks/shopify/gdpr`:
- customers/data_request
- customers/redact
- shop/redact

## Data Privacy (GDPR)
The compliance endpoint fulfills each request (it does not merely acknowledge):
- customers/redact — anonymizes matching Pivota order rows (name → redacted,
  email → a non-reversible tombstone) and scrubs stored webhook-event payloads.
- shop/redact — the same, scoped to the whole shop.
- customers/data_request — exports the data Pivota holds for the customer.
Every request is recorded in an audit table (`shopify_gdpr_requests`). Requests
are HMAC-verified; unsigned requests return 401.
Endpoint:
  https://api.pivota.cc/webhooks/shopify/gdpr

## Uninstall Handling
- We listen to `app/uninstalled` and:
  - Mark the store as disconnected
  - Remove stored access tokens

## Scopes Justification
App A requests exactly these read-only scopes (matching `[access_scopes]` in
`shopify.app.toml`):
- read_products — Sync the product catalog for order context and AI-readiness analysis.
- read_orders — Sync order status; support order attribution.
- read_fulfillments — Capture shipment/fulfillment updates and tracking numbers.
- read_discounts — Read Shopify-native discount metadata for quote validation and promotion display.

No write scopes are requested. Attribution and sync run entirely read-only.

## Support Contacts
- Support email: support@pivota.cc
- Support URL: [FILL_ME_IN]

## Notes for Reviewer
- The app uses OAuth (managed install) and does not require merchants to create
  custom apps or provide API keys.
- Webhook signatures are HMAC-verified against the app's client secret; the
  handler accepts either configured app secret (dual-app safe). Unsigned or
  mis-signed requests are rejected with 401.

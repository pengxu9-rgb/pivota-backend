# Shopify App Review Submission (Template)

## App Overview
- App name: Pivota Merchant
- App URL: https://api.pivota.cc
- OAuth redirect URL:
  - https://api.pivota.cc/integrations/shopify/oauth/callback

## Installation Instructions
1) Provide your Shopify MyShopify domain (e.g. `your-store.myshopify.com`).
2) Open the one-time install link we provide.
3) Approve the requested scopes.
4) After install, the app registers required webhooks and begins syncing order and fulfillment updates.

## Test Store / Reviewer Access
- Store domain (MyShopify): [FILL_ME_IN]
- Admin access (collaborator or staff account): [FILL_ME_IN]
- Notes: [FILL_ME_IN]

## Core Functionality (What to Verify)
- Orders and fulfillment updates sync into Pivota:
  - Update a fulfillment / tracking number in Shopify.
  - Pivota order status should move to shipped and show tracking_number.
- Webhooks are automatically registered after OAuth install:
  - orders/create
  - orders/updated
  - orders/paid
  - orders/cancelled
  - fulfillments/create
  - fulfillments/update
  - orders/fulfilled
  - app/uninstalled

## Data Privacy (GDPR)
Configured in Partner Dashboard to the static endpoint below:
- customers/data_request
- customers/redact
- shop/redact
Endpoint:
  https://api.pivota.cc/webhooks/shopify/gdpr

## Uninstall Handling
- We listen to `app/uninstalled` webhook and:
  - Mark the store as disconnected
  - Remove stored access tokens

## Scopes Justification
Provide a short reason for each scope requested. Example:
- read_orders / write_orders: Sync order status and fulfillment state.
- read_fulfillments: Capture shipment updates and tracking numbers.
- read_customers: Support customer matching for post-purchase communication (if enabled).
- read_products: Sync product catalog for order context (if enabled).
- read_discounts: Read Shopify-native discount metadata for quote validation and promotion display.
- read_returns: Process return events (if enabled).
- read_shopify_payments_disputes: Track dispute events (if enabled).
- read_shopify_payments_payouts: Reconcile payouts (if enabled).
- read_legal_policies / read_content: Read store policies for compliance display (if enabled).

## Support Contacts
- Support email: support@pivota.cc
- Support URL: [FILL_ME_IN]

## Notes for Reviewer
- The app uses OAuth and does not require merchants to create custom apps or provide API keys.
- Webhook signatures are verified using the app's client secret.

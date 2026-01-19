# Pivota Merchant App — FAQ

Last updated: 2026-01-19

## Do I need to create a separate app or paste API keys?
No. The recommended setup uses the Pivota Merchant App authorization flow. You approve permissions, and Pivota stores the required access token securely to operate the integration.

If the public installation flow is temporarily unavailable (for example, during review), Pivota may provide a fallback option that uses a manually generated Admin API access token.

## Why do you need access to orders and fulfillments?
Pivota syncs order and fulfillment status (including tracking updates) so your Pivota order state stays consistent with your store admin.

## How do webhooks work?
After authorization, the app registers webhooks for order and fulfillment topics. Webhooks are verified using HMAC signatures to ensure authenticity.

## What happens when I uninstall the app?
When the platform notifies us that the app was uninstalled, Pivota disconnects the store and clears stored access tokens for that integration.

## How do I request data deletion?
Email `support@pivota.cc` with your merchant ID and store domain. If you are subject to data protection laws, the platform may also submit compliance requests automatically; Pivota supports these requests.


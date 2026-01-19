# Pivota Merchant App — Troubleshooting

Last updated: 2026-01-19

## Installation issues

### “Installation link is invalid”
- Install links are one‑time use and expire after a short period.
- Generate a new link and complete authorization promptly.
- Use the store’s canonical `*.myshopify.com` domain when asked for the store domain.

### Authorization succeeded but store does not appear connected
- Return to the Pivota Merchant Portal → Integrations and refresh the stores list.
- If it still does not appear, contact support with the store domain and timestamp.

## Sync issues

### Product sync shows 0 products
This typically indicates a permission or token problem (401/403).
- Re-authorize and retry.
- If you are using a manually generated Admin API token, regenerate the token and reconnect.

### Webhook events are not arriving
- Confirm webhook endpoints are configured and reachable.
- Confirm webhook signature verification is enabled and the correct secret is configured.
- Contact support with the webhook topic and approximate timestamp.

## Support
Email `support@pivota.cc` and include:
- Merchant ID
- Store domain
- Approximate timestamp
- Any error message shown


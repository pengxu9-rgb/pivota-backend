# Pivota Merchant App — Help & Resources

Last updated: 2026-01-19

This page provides self‑serve resources for merchants using the Pivota Merchant App.

## Quickstart
1) Connect your store in the Pivota Merchant Portal → Integrations.
2) Complete the authorization flow.
3) Confirm the store shows as connected.
4) Create or update a fulfillment with a tracking number in your store admin.
5) Confirm the matching Pivota order shows `shipped` with `tracking_number`.

## Common issues

### “Installation link is invalid”
- Install links are one‑time use and expire after a short period.
- Generate a new link and complete the authorization promptly.
- Ensure you use the store’s canonical `*.myshopify.com` domain when asked for the store domain.

### Product sync shows 0 products
This usually means the token used for Admin API calls is invalid/expired or missing permissions (401/403).
- Reconnect the store (re-authorize) and try sync again.
- If you are using a custom app token, regenerate the token and reconnect.

## Data privacy and compliance requests
Pivota supports mandatory compliance webhooks for data requests and redaction requests sent by the platform.
If you have questions about a specific request, contact `support@pivota.cc` with the request timestamp and store domain.

## Uninstall / disconnect
If you uninstall the app:
- Pivota stops receiving store events.
- Stored access tokens for the integration are removed/cleared where applicable.

## Support
- Email: `support@pivota.cc`
- Please include: merchant ID, store domain, approximate time of the issue, and any error message shown in the portal.


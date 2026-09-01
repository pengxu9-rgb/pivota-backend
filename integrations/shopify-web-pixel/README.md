# Pivota Shopify Web Pixel

This extension maps Shopify standard customer events into Pivota's existing
canonical commerce ledger. It never emits authoritative order, payment, or
refund facts; those remain verified webhook/PSP events.

## Scaffold and deploy

Shopify owns the extension `uid`. Do not deploy the checked-in TOML template or
replace its placeholder by hand. From the repository root, use the latest
Shopify CLI and the app declared in `shopify.app.toml`:

```text
shopify app generate extension --template web_pixel --name pivota-commerce-telemetry
```

Keep the generated `uid`, copy the privacy/settings sections from
`shopify.extension.toml.template`, and copy `src/index.js` plus `src/mapper.mjs`
into the generated extension. Then deploy the app version through Shopify CLI.

The app requests `write_pixels` and `read_customer_events`. Existing installs
must approve the new scopes before activation.

## Per-store settings

An authenticated merchant provisions settings with:

```text
POST /merchant-events/v1/shopify-pixel/install-token
{"store_id":"store_...","ttl_days":90}
```

The response's `web_pixel_settings` object is the exact settings input for
Shopify Admin GraphQL `webPixelCreate` or `webPixelUpdate`. The token has a
Shopify-pixel-specific JWT audience and is bound to one active Shopify store.
Because strict pixel sandboxes don't provide a merchant storefront Origin that
Pivota can reliably bind, this token is not origin-bound. Its authority is
therefore narrower than a merchant server key: it can submit only the public
front-funnel allowlist and cannot submit amounts, buyer/order/refund IDs, or
terminal commerce events.

The receiving endpoint is:

```text
POST /merchant-events/v1/shopify-pixel/batch
```

`checkout_completed` intentionally maps to `checkout.submitted`. Verified
Shopify order webhooks and PSP webhooks close order/payment/refund truth.

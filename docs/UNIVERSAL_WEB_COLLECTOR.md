# Universal Web Collector

The Universal Web Collector supplies browser/session telemetry for storefronts
whose native webhooks observe only the server-side order lifecycle. It is an
additive producer for the existing `MerchantCommerceEvent` ledger, not a new
analytics pipeline.

## Security boundary

Never put a merchant API key in browser code. The existing
`POST /merchant-events/v1/batch` endpoint remains for trusted server collectors
and native adapters that can calculate the merchant HMAC.

An authenticated merchant instead provisions an origin-bound public write token:

```http
POST /merchant-events/v1/web/install-token
Authorization: Bearer <merchant JWT>
Content-Type: application/json

{
  "store_id": "store_123",
  "allowed_origins": [
    "https://shop.example.com",
    "https://checkout.example.com"
  ],
  "ttl_days": 90
}
```

If `allowed_origins` is omitted, Pivota uses the connected store domain when it
is a valid HTTPS origin. Specify it explicitly for Wix site IDs, Shopify custom
domains, separate checkout hosts, and headless storefronts. HTTP is accepted
only for localhost development.

The returned token is public in the same sense as a browser analytics write key:
it does not grant reads, merchant administration, server event ingestion, or
payment execution. It is bound to one merchant, store, platform, origin set, and
expiry. Disconnecting the store rejects existing tokens immediately. A separate
`MERCHANT_WEB_COLLECTOR_SIGNING_SECRET` may be configured; otherwise Pivota uses
a domain-separated key derived from `JWT_SECRET_KEY`. An insecure/default JWT
secret fails closed.

The public endpoint accepts only non-authoritative funnel observations:

```text
agent.requested
search.performed
product.viewed
cart.created
cart.item_added
cart.item_removed
cart.updated
checkout.started
checkout.submitted
payment.attempted
```

It rejects payment success/failure, orders, refunds, returns, money amounts,
buyer IDs, and order/refund/return IDs. Those facts must still come from a signed
native webhook or trusted Server Collector. Events may be at most seven days old
or five minutes in the future.

## Install

### Custom/headless self-service onboarding

An authenticated merchant can create the required store scope and provision the
first collector token in one request; no fake platform API credential is needed:

```http
POST /integrations/custom/connect
Authorization: Bearer <merchant JWT>
Content-Type: application/json

{
  "merchant_id": "merchant_123",
  "store_url": "https://shop.example.com",
  "store_name": "Example Headless Store",
  "allowed_origins": ["https://checkout.example.com"],
  "collector_token_ttl_days": 90
}
```

The storefront origin is always added to the allowlist. The operation is
idempotent by merchant and normalized storefront origin: reconnecting reactivates
the existing `merchant_stores` row and rotates the public collector token instead
of creating a second store. The endpoint accepts HTTPS origins only (with the
existing localhost development exception), performs tenant authorization, and
provisions the token before writing the store so missing signing configuration
cannot leave a half-connected record.

The response includes `store_id`, `install_snippet`, `collector_token`, expiry,
and the trusted Server Collector path. Subsequent browser-token rotation can use
`POST /merchant-events/v1/web/install-token`.

The provisioning response returns `install_snippet`. It intentionally starts in
pending-consent mode:

```html
<script
  async
  src="https://api.pivota.cc/merchant-events/v1/collector.js"
  data-pivota-token="<public origin-bound token>"
  data-pivota-consent="pending">
</script>
```

Add `https://api.pivota.cc` to the storefront's CSP `script-src` and
`connect-src`. Do not place the token in a query parameter or server logs.

Connect the merchant's consent manager before tracking:

```js
PivotaCommerce.setConsent("granted");
// Or, after a rejection:
PivotaCommerce.setConsent("denied");
```

The collector creates a pseudonymous visitor ID in `localStorage` and a session
ID in `sessionStorage` only after consent is granted. Denial deletes those IDs,
the attribution context, and queued events. The collector never reads or sends
page URL, referrer, cookies, IP address, user agent, names, email, phone, address,
or payment-card fields.

## Event API

```js
PivotaCommerce.track("product.viewed", {
  canonical_product_id: "prod_123",
  canonical_variant_id: "var_456"
});

PivotaCommerce.track("cart.item_added", {
  cart_id: "cart_789",
  canonical_product_id: "prod_123",
  canonical_variant_id: "var_456",
  metadata: { quantity: 2 }
});

PivotaCommerce.track("checkout.started", {
  cart_id: "cart_789",
  checkout_id: "checkout_012"
});
```

Carry every identifier known at each step. In particular:

```text
session_id + cart_id
             cart_id + checkout_id
                       checkout_id + payment_id
```

The native order/payment webhook should then carry the overlapping payment,
checkout, cart, or click identifier that bridges to `order_id`.

The collector recognizes Pivota attribution query parameters such as
`pivota_click_id`, `pvt_click_id`, `pivota_agent_id`, `pivota_source_channel`,
`pivota_protocol`, `pivota_llm_provider`, and `pivota_llm_model`. `utm_content`
is accepted as a click ID only when it matches the Pivota `clk_*` shape. This
context is session-scoped and remains `merchant_asserted`, not cryptographically
verified agent identity.

For a PDP whose canonical IDs are already rendered into the page, declarative
auto-tracking is available after consent:

```html
<script
  async
  src="https://api.pivota.cc/merchant-events/v1/collector.js"
  data-pivota-token="<token>"
  data-pivota-consent="granted"
  data-pivota-auto-page="true"
  data-pivota-product-id="prod_123"
  data-pivota-variant-id="var_456">
</script>
```

For regulated deployments, keep the initial value `pending` and grant consent
through the merchant CMP rather than using the declarative `granted` mode.

## Delivery behavior

- Events retain their client-generated `event_id` across retries, so the
  canonical ledger remains idempotent.
- The runtime batches up to 20 events, queues at most 100 per tab, retries failed
  fetches up to five times, flushes when connectivity returns, and uses
  `sendBeacon` during `pagehide`.
- The browser body uses `text/plain` to avoid exposing a credential in a custom
  header or URL. The server still parses and validates strict JSON.
- The endpoint re-checks that the bound `merchant_stores` row remains active on
  every batch.

## Platform usage

- Cafe24 Data Bridge already supplies product/checkout events. Use this collector
  only for gaps such as cart remove/update or custom storefronts.
- WooCommerce, Adobe Commerce, SHOPLINE, Shoplazza, and SFCC use it for product,
  search, session, cart, and storefront-specific checkout observations while
  retaining native signed webhooks for transaction closure.
- Shopify uses the dedicated strict-sandbox bridge under
  `integrations/shopify-web-pixel/` and
  `POST /merchant-events/v1/shopify-pixel/batch`. Its store-bound public token
  has a separate audience because a strict pixel sandbox cannot reliably supply
  the merchant storefront Origin required by the generic collector.
- Custom/headless stores use `POST /integrations/custom/connect` to create their
  active store scope and receive the consent-pending installation snippet.

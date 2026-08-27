# SHOPLINE and Shoplazza adapters

Both platforms are implemented as thin native catalog sources over the existing
Universal Product Sync and `StandardProduct` contracts.

```text
POST /integrations/shopline/connect
POST /integrations/shoplazza/connect
POST /products/sync-universal/
```

SHOPLINE uses the REST Admin API with Bearer authentication, the
`read_products` scope, embedded variants, and `Link`/`page_info` pagination. The
default version is the current stable `v20260601`, but the version is stored per
connection. The manual connection route accepts merchant-supplied private-app or
externally managed OAuth tokens. It does not claim to refresh ten-hour OAuth
tokens without the app key/secret and refresh metadata.

Shoplazza uses `/openapi/2026-01/products`, the `Access-Token` header, embedded
variants, and cursor pagination. Both mappers retain native product/variant IDs,
price, compare-at price, stock, images, handles, and storefront URLs. Inventory
is conservative: zero inventory is sellable only when the platform explicitly
disables tracking or allows continued selling.

The source registry declares `catalog_pull=true`. Signed order lifecycle
webhooks feed the same canonical merchant-event ledger used by all other
adapters:

```text
POST /webhooks/shopline/{store_id}
POST /webhooks/shoplazza/{store_id}
```

Pass the app Client Secret as optional `app_secret` when connecting the store,
then subscribe the platform to the topics returned as
`required_webhook_topics`. SHOPLINE maps order create/paid/cancelled and only
maps `refunds/create` when its payload contains an explicit successful refund
transaction; SHOPLINE documents that refund-form creation alone is unrelated to
funds movement. Shoplazza maps order create/paid/cancelled plus partial/full
refund notifications. Delivery IDs become retry-stable trace/idempotency keys.

With `SHOPLINE_WEBHOOK_BASE_URL`, `SHOPLAZZA_WEBHOOK_BASE_URL`, or the shared
`PUBLIC_BASE_URL` configured as an HTTPS public origin, authenticated merchants
can install any missing subscriptions idempotently:

```text
POST /integrations/shopline/{store_id}/webhooks/ensure
POST /integrations/shoplazza/{store_id}/webhooks/ensure
```

The installer lists current subscriptions first and creates only missing
topic-and-callback pairs. A retry after a partial upstream failure is therefore
safe. A per-store `app_secret` may be omitted when the deployment supplies
`SHOPLINE_APP_SECRET` or `SHOPLAZZA_CLIENT_SECRET` centrally.

Native payloads are reduced to an allowlist of order, payment, product/variant,
quantity, status, amount, and attribution IDs. Names, email, phone, addresses,
IP addresses, arbitrary item properties, and gateway receipts are not copied to
the canonical ledger.

Product views, reliable cart/checkout behavior, and custom storefront session
identity still use the Universal Web/Server Collector and
`/merchant-events/v1/batch`. The webhooks complement that collector; they do
not replace browser/session instrumentation.

The adapters are deliberately not added to live-SKU, checkout/readiness, or
public-PDP renderability allowlists. Those contracts require an upstream
single-SKU validator, an executable checkout path, or an end-to-end measured
PDP lane respectively; a successful catalog pull proves none of them.

Both endpoints validate the platform's base64 HMAC-SHA256 over the exact raw
body with a timing-safe comparison, verify the source store domain, cap payloads
at 1 MB, and acknowledge unsupported topics without recording them.

## Official references

- https://developer.shopline.com/docs/apps/api-instructions-for-use/rest-admin-api/overview
- https://developer.shopline.com/docs/admin-rest-api/product/product/get-products/
- https://developer.shopline.com/docs/apps/api-instructions-for-use/webhooks/overview/
- https://developer.shopline.com/docs/admin-rest-api/webhook/subscribe-to-a-webhook/
- https://developer.shopline.com/docs/admin-rest-api/webhook/get-a-list-of-subscribed-webhooks/
- https://developer.shopline.com/docs/webhook/order/order-created/
- https://developer.shopline.com/docs/webhook/order/refund-created
- https://www.shoplazza.dev/api/openapi
- https://www.shoplazza.dev/api/products
- https://www.shoplazza.dev/docs/app/building-blocks/webhooks/overview
- https://www.shoplazza.dev/docs/app/building-blocks/webhooks/supported-webhook-events
- https://www.shoplazza.dev/api/webhooks
- https://www.shoplazza.dev/api/webhook-create

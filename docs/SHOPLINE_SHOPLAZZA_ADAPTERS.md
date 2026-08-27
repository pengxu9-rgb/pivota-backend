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

The source registry currently declares only `catalog_pull=true`. Product/order
webhook delivery is not claimed by these catalog credentials. Until native
event subscriptions and app-secret verification are connected, product views,
cart, checkout, payment, and order telemetry use the Universal Web/Server
Collector and `/merchant-events/v1/batch`.

The adapters are deliberately not added to live-SKU, checkout/readiness, or
public-PDP renderability allowlists. Those contracts require an upstream
single-SKU validator, an executable checkout path, or an end-to-end measured
PDP lane respectively; a successful catalog pull proves none of them.

Both platforms publish official signed webhook mechanisms. SHOPLINE uses an
HMAC-SHA256 body signature and a stable webhook message ID; Shoplazza uses a
base64-encoded HMAC-SHA256 body signature and a retry-stable deduplication ID.
Those fit the existing canonical event adapter without requiring a new event
bus and are the next incremental step.

## Official references

- https://developer.shopline.com/docs/apps/api-instructions-for-use/rest-admin-api/overview
- https://developer.shopline.com/docs/admin-rest-api/product/product/get-products/
- https://developer.shopline.com/docs/apps/api-instructions-for-use/webhooks/overview/
- https://www.shoplazza.dev/api/openapi
- https://www.shoplazza.dev/api/products
- https://www.shoplazza.dev/docs/app/building-blocks/webhooks/overview

# Salesforce B2C Commerce adapter

This is a thin native catalog adapter over the existing Universal Product Sync
and `StandardProduct` contracts. It uses current Salesforce Commerce APIs
(SCAPI), not deprecated OCAPI.

```text
POST /integrations/salesforce-commerce-cloud/connect
POST /products/sync-universal/
```

The connection requires the B2C Commerce short code, organization ID, site ID,
and a SLAS private client ID/secret with the minimum Shopper Search and Shopper
Products scopes. The adapter requests a site-bound guest token using
`grant_type=client_credentials` and `channel_id={site_id}`. Tokens are cached in
memory until shortly before expiry and never returned by the connection route.

Each catalog page uses Shopper Search without a keyword, which returns only
online products assigned to the site catalog. Search pages can contain up to
200 hits; the adapter hydrates them in groups of 24 through Shopper Products,
the documented multi-product limit, with price, availability, image, and
variation expansions. Exact ATS is retained when present; when SCAPI exposes
only `orderable=true`, inventory quantity `1` is an
explicit availability sentinel rather than a claimed physical stock count.

The capability registry still declares only `catalog_pull=true`; the registry's
`catalog_events` flag describes catalog-change subscriptions, not the separate
commerce telemetry ledger. Checkout, live quote, public PDP, and catalog event
subscriptions remain unclaimed.

## Native commerce telemetry

SFCC does not expose a general outbound order-webhook subscription comparable
to WooCommerce. Pivota therefore ships a small optional cartridge under
`integrations/sfcc-cartridge/`:

```text
SCAPI / OCAPI lifecycle hook
        -> local custom-object outbox
        -> scheduled drain job
        -> HMAC-SHA256 event batch
        -> POST /webhooks/salesforce-commerce-cloud/{store_id}
        -> MerchantCommerceEvent / canonical ledger
```

Provision the per-store signing secret with:

```text
POST /integrations/salesforce-commerce-cloud/{store_id}/telemetry/provision
```

The secret is returned only on first provision or explicit rotation. Re-running
the catalog connection preserves it. The receiver binds the signature to the
raw body and a five-minute timestamp window, checks the exact configured site
ID, accepts at most 100 events / 1 MB, and relies on the canonical ledger for
idempotency.

The included hooks cover cart creation, item addition, checkout submission,
order creation, and payment authorization/decline for supported SCAPI/OCAPI flows.
They only append a PII-free local outbox record. Network delivery happens in a
scheduled job, so Pivota availability cannot add network latency to checkout.
The order-submission hooks carry the originating basket ID into the order event
so the canonical ledger can stitch cart, checkout, and order without inference.
SFRA/SiteGenesis flows that do not use the corresponding Shopper APIs require a
one-line call to the cartridge helper from the merchant's existing checkout
extension. Settlement, cancellation, and refund events must come from the
merchant's authoritative order/payment workflow; the adapter deliberately does
not infer payment success from order creation or authorization.

The Universal Web/Server Collectors remain valid in parallel and are the
preferred coverage for product views, search, session identity, and any theme-
or storefront-specific interaction that SFCC lifecycle hooks cannot observe.

## Official references

- https://developer.salesforce.com/docs/commerce/commerce-api/guide/scapi-get-started.html
- https://developer.salesforce.com/docs/commerce/commerce-api/guide/slas-private-client.html
- https://developer.salesforce.com/docs/commerce/commerce-api/guide/base-url.html
- https://developer.salesforce.com/docs/commerce/commerce-api/guide/extensibility_via_hooks.html
- https://developer.salesforce.com/docs/commerce/b2c-commerce/guide/b2c-custom-job-steps.html
- https://developer.salesforce.com/docs/commerce/b2c-commerce/guide/b2c-webservices.html
- https://developer.salesforce.com/docs/commerce/b2c-commerce/references/b2c-script-api/dw.svc.Service.html
- https://developer.salesforce.com/docs/commerce/b2c-commerce/references/b2c-script-api/dw.util.Bytes.html
- https://salesforcecommercecloud.github.io/commerce-sdk-isomorphic/classes/shopperproducts.shopperproducts-3.html
- https://salesforcecommercecloud.github.io/pwa-kit/classes/_internal_.ShopperSearch.html

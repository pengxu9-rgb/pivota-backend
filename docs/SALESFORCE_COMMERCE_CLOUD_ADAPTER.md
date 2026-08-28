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

The capability registry declares only `catalog_pull=true`. SCAPI shopper basket
and order endpoints make a future native checkout adapter possible, but this
phase does not claim checkout, live quote, payment, webhook, public PDP, or
event-subscription support. The Universal Web/Server Collector covers telemetry
until those contracts are separately implemented and verified.

## Official references

- https://developer.salesforce.com/docs/commerce/commerce-api/guide/scapi-get-started.html
- https://developer.salesforce.com/docs/commerce/commerce-api/guide/slas-private-client.html
- https://developer.salesforce.com/docs/commerce/commerce-api/guide/base-url.html
- https://salesforcecommercecloud.github.io/commerce-sdk-isomorphic/classes/shopperproducts.shopperproducts-3.html
- https://salesforcecommercecloud.github.io/pwa-kit/classes/_internal_.ShopperSearch.html

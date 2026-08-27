# Magento / Adobe Commerce adapter

## Current scope

The native adapter covers Adobe Commerce PaaS/on-premises and Magento Open
Source. It uses a merchant-created Integration Access Token and keeps the core
catalog and telemetry contracts platform-neutral.

```text
POST /integrations/magento/connect
POST /products/sync-universal/
GET  /integrations/magento/{store_id}/status
```

Catalog synchronization uses `GET /rest/{store_view}/V1/products` with
`searchCriteria` pagination. Configurable products additionally read
`GET /rest/{store_view}/V1/configurable-products/{sku}/children`. Products and
children are mapped into `StandardProduct` and `StandardProductVariant`, then
flow through the existing universal catalog ingest without a Magento-specific
core path.

The adapter treats missing inventory data conservatively: a product is not
declared orderable unless Magento returns usable stock state. Access tokens are
stored only in the existing merchant-store credential blob and are never
returned by status endpoints.

## Telemetry boundary

The first release uses the existing Universal Web/Server Collector and
`/merchant-events/v1/batch` for product, cart, checkout, payment, and order
telemetry. It does not claim that a catalog token automatically configures
native event delivery.

Adobe Commerce Webhooks and Adobe I/O Events both require Commerce-side event
subscription/configuration. A follow-up native event adapter can map the
selected observer/plugin events into the same `MerchantCommerceEvent` contract;
it does not require a new event bus.

Adobe Commerce as a Cloud Service is also a separate authentication increment:
it uses IMS OAuth 2 server-to-server credentials plus Adobe organization/API
headers and a different REST base URL. Those credentials are deliberately not
accepted by the PaaS Integration Token connection route.

## Official references

- https://developer.adobe.com/commerce/webapi/rest/
- https://developer.adobe.com/commerce/webapi/get-started/authentication/
- https://developer.adobe.com/commerce/webapi/rest/use-rest/performing-searches
- https://developer.adobe.com/commerce/webapi/rest/tutorials/orders/order-add-items
- https://developer.adobe.com/commerce/extensibility/events/
- https://developer.adobe.com/commerce/extensibility/webhooks/api
- https://developer.adobe.com/commerce/webapi/rest/authentication/server-to-server

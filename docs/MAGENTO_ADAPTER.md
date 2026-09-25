# Magento / Adobe Commerce adapter

## Current scope

The native adapter covers Adobe Commerce PaaS/on-premises and Magento Open
Source. It uses a merchant-created Integration Access Token and keeps the core
catalog and telemetry contracts platform-neutral.

```text
POST /integrations/magento/connect
POST /products/sync-universal/
GET  /integrations/magento/{store_id}/status
GET  /webhooks/adobe-commerce/{store_id}?challenge=...
POST /webhooks/adobe-commerce/{store_id}
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

Universal Web/Server Collector and `/merchant-events/v1/batch` remain the
coverage path for browser product, cart, checkout, session, and agent-identity
events. The native Adobe I/O Events receiver adds asynchronous order, payment,
and refund closure without changing the canonical event bus.

Set both `adobe_io_client_id` and the store-specific `adobe_io_provider_id` on
`POST /integrations/magento/connect`, register the returned
`adobe_io_webhook_path` in Adobe Developer Console, and subscribe the Commerce
event provider to these observer events:

- `observer.checkout_submit_all_after`
- `observer.sales_order_save_after`
- `observer.sales_order_invoice_save_after`
- `observer.sales_order_creditmemo_save_after`

The receiver supports Adobe's GET challenge, verifies that every CloudEvent is
addressed to the configured `recipientclientid` and signed provider `source`,
and validates at least one RSA-SHA256 Adobe I/O signature. Public keys are
accepted only from the fixed `static.adobeioevents.com/prod/keys/` namespace
and cached for less than 24 hours. Single-event and batches of up to 100
CloudEvents are supported.

The mapper persists only allowlisted commerce facts. It never retains address,
email, arbitrary extension attributes, or payment-card fields present in the
full Commerce observer payload. Paid invoices create `payment.succeeded`; fully
paid orders create `order.paid`; successful credit memos create
`refund.succeeded`. All facts carry the native order ID into the existing
`MerchantCommerceEvent` ledger for correlation.

Configure each Commerce subscription with only the fields needed by the mapper:

- checkout/order: `entity_id`, `increment_id`, `state`, `status`,
  `grand_total`, `total_paid`, `total_due`, `order_currency_code`,
  `customer_id`, `payment.last_trans_id`, and item `item_id`, `product_id`,
  `sku`, `qty_ordered`, `price`, `row_total`, `row_total_incl_tax`
- invoice: `entity_id`, `order_id`, `state`, `transaction_id`, `grand_total`,
  `order_currency_code`, and the same safe item commerce fields
- credit memo: `entity_id`, `increment_id`, `order_id`, `state`,
  `transaction_id`, `grand_total`, `order_currency_code`, and the same safe item
  commerce fields

Do not subscribe address, email, payment-card, or arbitrary extension-attribute
fields.

Adobe Commerce Webhooks are deliberately not used for telemetry because they
are synchronous interception/validation hooks and can affect the originating
Commerce operation. Adobe I/O subscription and Developer Console registration
remain merchant-side setup; the catalog Integration Token alone cannot create
that registration.

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
- https://developer.adobe.com/commerce/extensibility/events/events-reference
- https://developer.adobe.com/events/docs/guides/
- https://developer.adobe.com/commerce/extensibility/webhooks/api
- https://developer.adobe.com/commerce/webapi/rest/authentication/server-to-server

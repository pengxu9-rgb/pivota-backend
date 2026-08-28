# WooCommerce native telemetry

WooCommerce already had native product sync, store connection, order writeback,
external-checkout redirect, and an attributed conversion poller. The native
telemetry increment adds a signed webhook adapter into the same canonical event
ledger used by Cafe24 and universal collectors.

```text
POST /webhooks/woocommerce/{store_id}
```

Configure these core WooCommerce webhook topics:

- `order.created`
- `order.updated`

Set the Delivery URL to the store-specific path returned by
`POST /integrations/woocommerce/connect`. Set the webhook Secret to the optional
`webhook_secret` supplied during connection. If it is omitted in both places,
WooCommerce defaults the signature secret to the API user's consumer secret and
Pivota uses the connected consumer secret as the compatibility fallback.

The receiver verifies the official base64-encoded HMAC-SHA256 signature over the
raw request body, checks the reported source host, and accepts only active
WooCommerce store IDs. It maps order status into `order.created`, `order.paid`,
`order.cancelled`, `payment.failed`, and `refund.succeeded`. Event IDs are based
on the store, canonical lifecycle fact, and order/payment entity, so duplicate
`order.created` and `order.updated` deliveries are idempotent.

Only allowlisted order facts and line-item identifiers are copied to canonical
metadata. Billing/shipping addresses, email, phone, and customer names are not
stored. WooCommerce 8.5+ Order Attribution `utm_content` metadata is reused as a
`click_id` only when it matches Pivota's `clk_*` identifier shape.

Cart item add/remove/update remain Universal Web Collector coverage. WooCommerce
can emit a custom `action.woocommerce_add_to_cart` webhook, but its core payload
is not a stable substitute for browser/session telemetry across themes and cart
extensions.

## Official references

- https://woocommerce.com/document/webhooks/
- https://developer.woocommerce.com/docs/apis/rest-api/v2/webhooks
- https://woocommerce.github.io/code-reference/classes/WC-Webhook.html

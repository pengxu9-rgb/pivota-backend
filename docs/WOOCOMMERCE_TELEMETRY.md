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

After connecting the store, Pivota can install or repair both subscriptions
idempotently through the WooCommerce REST API:

```text
POST /integrations/woocommerce/{store_id}/webhooks/ensure
```

Set `WOOCOMMERCE_WEBHOOK_BASE_URL` or `PUBLIC_BASE_URL` to Pivota's public HTTPS
origin. The installer lists existing webhooks, creates missing topic-and-callback
pairs, and re-synchronizes the signing secret and active status on matches. This
also makes a webhook-secret rotation recoverable without deleting subscriptions
by hand. WooCommerce API credentials are sent with HTTPS Basic authentication;
they are never placed in query strings or error responses. The connected REST
API key needs read/write permission to list and create webhooks.

Set the Delivery URL to the store-specific path returned by
`POST /integrations/woocommerce/connect`. Set the webhook Secret to the optional
`webhook_secret` supplied during connection. When the installer is used, Pivota
sets that secret explicitly; if it was omitted at connection time, Pivota
explicitly uses the connected consumer secret as the compatibility fallback.
Manual subscriptions must use that same value rather than relying on the
platform's default secret.

The receiver verifies the official base64-encoded HMAC-SHA256 signature over the
raw request body, checks the reported source host, and accepts only active
WooCommerce store IDs. It maps order status into `order.created`, `order.paid`,
`order.cancelled`, `payment.failed`, and `refund.succeeded`. Event IDs are based
on the store, canonical lifecycle fact, and order/payment entity, so duplicate
`order.created` and `order.updated` deliveries are idempotent.

WooCommerce has no refund webhook topic, so refunds are read from the order's
`refunds[]` array rather than from its status. Every entry with a native refund
id becomes its own `refund.succeeded` — keyed on that id, amounted from the
entry's `total` as a positive magnitude in `currency_minor_unit` — emitted in
addition to the order lifecycle events of the same delivery. That is what makes a
partial refund visible at all: it arrives as an `order.updated` on a still
`processing`/`completed` order. The cumulative `total_refunded` is used only as a
legacy fallback, when a `refunded` order carries no usable `refunds[]` entry.
`refunds[].reason` is merchant free text and is never stored.

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

# Universal Commerce Event Ingestion

`POST /merchant-events/v1/batch` is the platform-neutral write contract for store
adapters and Universal Server Collectors. It appends normalized events to the
existing `commerce_interactions` / `commerce_interaction_events` ledger; it is not
a second analytics pipeline.

## Authentication and retries

Send the merchant id in `X-Pivota-Merchant-Id` and the lowercase hex HMAC-SHA256
of the exact request bytes in `X-Pivota-Signature`. The HMAC key is the merchant
API key. A request may contain 1–100 events and is limited to 1 MB.

Every event must have an upstream-stable `event_id`. Pivota uses it as the ledger
idempotency key, so callers may retry a batch after a timeout. The response reports
accepted and duplicate counts per event.

## Canonical event names

- Discovery: `agent.requested`, `search.performed`, `product.viewed`
- Cart: `cart.created`, `cart.item_added`, `cart.item_removed`, `cart.updated`
- Checkout: `checkout.started`, `checkout.submitted`
- Payment: `payment.attempted`, `payment.authorized`, `payment.declined`,
  `payment.succeeded`, `payment.failed`
- Order: `order.created`, `order.paid`, `order.cancelled`
- After-sales: `refund.created`, `refund.succeeded`, `return.created`,
  `return.completed`

Platform-native webhook names must be mapped to these values in the adapter, not
sent as new event names.

## Stitching contract

Each event must carry at least one correlation key. Adapters should carry forward
all keys known at that funnel step so the ledger can bridge identifiers:

```text
session_id + cart_id
            cart_id + checkout_id
                      checkout_id + payment_id
                                    payment_id + order_id
                                                 order_id + refund_id
```

`merchant_id` is taken only from the authenticated API key. `store_id` scopes
store-local session, cart, quote, checkout, payment, order, refund, and return
identifiers for merchants with multiple stores.
`visitor_id` is retained for analysis but is not used by itself to merge sessions.

Agent identity received through this merchant-signed endpoint is stored as
`merchant_asserted`; it is not equivalent to a cryptographically verified agent.

## Metadata safety

`metadata` is a bounded analytics extension, not an arbitrary native-payload
archive. The collector accepts only documented commerce fields such as
`quantity`, `native_topic`, allowlisted native status/amount fields, sanitized
line items/products, and webhook delivery identifiers. Unknown top-level keys
are rejected, as are sensitive keys (including email, phone, address, IP,
credentials, cookies, and payment-card data) at any nesting depth.

Adapters must reduce native payloads to the safe vocabulary before ingestion.
Buyer contact, shipping/billing details, raw headers, credentials, and full
webhook payloads must never be placed in this ledger.

## Merchant funnel reads

Canonical adapter events are included in the existing merchant analytics API:

```text
GET /merchant/analytics/commerce-funnel
GET /merchant/analytics/commerce-funnel?group_by=platform
GET /merchant/analytics/commerce-funnel?group_by=store&platform=cafe24
```

The response keeps the original listing/click/attribution fields intact and
adds `event_funnel`, `ledger_events_total`, `ledger_interactions_total`, and
`observed_*` fields. `attributed_orders` continues to mean orders connected to
a Pivota click; `observed_order_conversion` additionally includes native store
events that do not have a click attribution edge.

Reads are newest-first and bounded by `COMMERCE_FUNNEL_LEDGER_EVENT_LIMIT`
(default 50,000, minimum 100, maximum 200,000). The response sets
`event_funnel.truncated=true` when the bound was reached, so callers never
mistake a partial aggregate for a complete all-time count.
If the canonical event store is temporarily unavailable during rollout,
`event_funnel.available=false` distinguishes that state from a valid empty
funnel while the legacy analytics response remains available.
Migration `206_commerce_event_funnel_read_index.sql` builds the merchant,
platform, and store recency indexes concurrently; it must be applied before
enabling the endpoint for a high-volume ledger and intentionally does not run
inside the latency-sensitive startup schema guard.

The legacy click/attribution tables do not have a reliable platform or store
identity. When `platform` or `store_id` is supplied, their metrics are therefore
excluded—including unscoped listing counts—instead of being incorrectly
assigned. The response makes this explicit in
`metric_scopes.legacy_attribution`; canonical `event_funnel` and `observed_*`
metrics remain exactly scoped to those filters. Platform/store groupings expose
canonical event slices only, which is reported by `slices_grouped=false` for
the legacy metrics.

## Example

```json
{
  "events": [
    {
      "event_id": "cafe24:webhook:01J6...",
      "event_type": "order.paid",
      "occurred_at": "2026-08-26T12:34:56Z",
      "platform": "cafe24",
      "store_id": "mall_123",
      "session_id": "session_abc",
      "cart_id": "cart_456",
      "checkout_id": "checkout_789",
      "payment_id": "payment_012",
      "order_id": "order_345",
      "amount_cents": 2599,
      "currency": "USD",
      "metadata": {
        "native_topic": "payment.completed"
      }
    }
  ]
}
```

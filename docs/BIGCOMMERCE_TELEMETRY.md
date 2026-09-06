# BigCommerce native telemetry

BigCommerce already had catalog validation (`adapters/bigcommerce_adapter.py`),
store connection, and an order writeback. It had no commerce telemetry at all.
This increment adds a header-authenticated webhook bridge into the same
canonical event ledger used by Shopify, Cafe24, WooCommerce, and SHOPLINE.

```text
POST /webhooks/bigcommerce/{store_id}
```

Two BigCommerce facts shape the design, and neither has an analogue in the other
native bridges.

## 1. Deliveries are thin — the receiver must fetch the order

A delivery carries no order fields:

```json
{
  "scope": "store/order/created",
  "store_id": "1025646",
  "data": { "type": "order", "id": 250 },
  "hash": "3f9ea420af83450d7ef9f78b08c8af25b2213637",
  "producer": "stores/{store_hash}"
}
```

`services/bigcommerce_order_fetch.py` reads the order back with the store's own
access token:

- `GET /stores/{hash}/v2/orders/{id}` — always
- `GET /stores/{hash}/v3/orders/{id}/payment_actions/refunds` — only when the
  delivery is `store/order/refund/created`, or the order's `payment_status` is
  `refunded` / `partially refunded`

Both calls are bounded (15 s) and any non-2xx raises `BigCommerceOrderFetchError`,
which the route answers **503**. That is deliberate: BigCommerce retries a
non-2xx delivery at escalating intervals over roughly 48 hours, so answering 200
after a failed fetch would drop the event permanently. An unsupported scope is
answered `{"status": "ignored"}` **before** any fetch, so an unmapped event can
neither cost nor drive a BigCommerce API call.

v2's `refunded_amount` is documented as *"Always returns 0"*, so it is never
read as the refunded magnitude. Refund magnitudes come only from the v3 refund
objects.

## 2. There is no delivery signature — the credential is a registered header

BigCommerce does not sign webhook bodies. Authentication is whatever custom
`headers` map you register with the hook. Pivota therefore:

1. mints a per-store random secret (`secrets.token_urlsafe(32)`) at subscription
   time and persists it in the store's credential JSON as `webhook_secret`;
2. registers every hook with `{"headers": {"X-Pivota-Webhook-Secret": "<secret>"}}`;
3. compares the delivered header with `hmac.compare_digest` on receipt.

A shared secret in a header cannot bind a delivery to its **body**, so the
receiver additionally binds the delivery to the store: the payload's `producer`
(`stores/<hash>`) must equal the store's own `store_hash`. The order is then read
back from that same store's API, so even a forged body can only make Pivota
re-read an order the merchant already owns.

Auth chain, in order:

1. body ≤ 1 MB, else 413
2. store lookup: `platform = 'bigcommerce'` and status in (`active`, `connected`)
3. one shared **401** for: unknown store, inactive store, no provisioned
   `webhook_secret`, missing header, wrong header
4. `telemetry_ingress_route("bigcommerce_webhook")` identification and the
   `platform` rate-limit tier
5. JSON parse (400)
6. `producer` ↔ `store_hash` binding (**401** on mismatch)
7. supported scope, else `{"status": "ignored"}` — no fetch
8. order id present, else 422
9. order + refunds fetch, **503** on failure
10. map, then `ingest_merchant_event_batch(write_path="bigcommerce_webhook",
    agent_identity_confidence="platform_asserted")`

## Subscriptions

```text
POST /integrations/bigcommerce/{store_id}/webhooks/ensure
```

Same merchant authorization as the WooCommerce installer. Set
`BIGCOMMERCE_WEBHOOK_BASE_URL` or `PUBLIC_BASE_URL` to Pivota's public HTTPS
origin. `services/bigcommerce_webhook_subscriptions.py` lists `GET /v3/hooks`,
updates any hook already matching scope+destination in place (destination,
`headers`, `is_active`) so a secret rotation cannot silently leave deliveries
unverifiable, creates the missing scopes, and deactivates extra duplicates for
the same scope+destination rather than deleting them. Hooks pointing elsewhere
belong to other apps and are never touched. The response reports the callback
URL and the scopes; it never returns the secret.

Scopes registered:

- `store/order/created`
- `store/order/updated`
- `store/order/statusUpdated`
- `store/order/refund/created`

## Event mapping

| Order state (after fetch) | Canonical events | Amount | `occurred_at` |
| --- | --- | --- | --- |
| any supported scope | `order.created` | `total_inc_tax` | `date_created` |
| `payment_status` ∈ {`captured`, `paid`, `partially refunded`, `refunded`} | `order.paid` | `total_inc_tax` | `date_modified` |
| `status_id == 5` (Cancelled) | `order.cancelled` | `total_inc_tax` | `date_modified` |
| `status_id == 6` (Declined) or `payment_status == declined` | `payment.failed` | `total_inc_tax` | `date_modified` |
| each v3 refund with an `id` | one `refund.succeeded` | that refund's `total_amount` | that refund's `created` |

`authorized` and `capture pending` deliberately do **not** produce `order.paid`:
an authorization is a hold, not a payment. `partially refunded` and `refunded`
do, because a refund presupposes a capture; the refunded magnitude is carried by
the refund events, each keyed on its own native refund id, so two partial
refunds of one order are two distinct facts that dedupe individually across
repeated deliveries. `native_amount_semantics=native_refund_total` marks them
per-refund rather than cumulative.

Event ids are `_entity_event_id(store_id, event_type, <native entity>)` — the
order id for the order events, the native refund id for refunds — so a repeated
`store/order/updated` produces byte-identical ids and dedupes at the ledger.

Order dates are RFC-2822 (`Tue, 05 Mar 2019 21:40:11 +0000`); refund `created`
is ISO 8601. Both spellings are accepted for either field so a fixture from one
API version cannot silently fall through to "now". Money is parsed with `Decimal`
and `ROUND_HALF_UP`, with zero-decimal currencies kept in whole units.

## `order_ref` — every BigCommerce order is platform-originated

`order_ref` is always `bigcommerce:<native order id>`.

Pivota's BigCommerce order writeback
(`routes/order_routes.py::create_bigcommerce_order`) *does* exist, but unlike the
WooCommerce writeback it records the Pivota order id only in `customer_message`
and `staff_notes` — both free text, and `customer_message` is filled in by the
**buyer** at checkout. Reading either would let a shopper forge a `pivota:` claim
and merge their order into someone else's interaction, so neither is read.
Recovering the Pivota identity needs a structured, non-buyer-writable marker (a
`pivota` v3 order metafield stamped at writeback and read back here); until that
exists, a Pivota-originated BigCommerce order is counted under its BigCommerce
identity.

## Privacy

Canonical metadata carries `native_topic` (the scope), `native_status`,
`native_payment_method`, `native_total_tax`, `webhook_delivery_id` (the delivery
`hash`), and `native_amount_semantics` — all already in
`ALLOWED_MERCHANT_METADATA_KEYS`; the allowlist was not widened. Billing and
shipping addresses, email, phone, and customer names are never stored. A
refund's `reason` is merchant free text and is never copied.

`native_line_items` is **not** populated: the v2 order body does not embed its
products, it exposes them as a `products` sub-resource URL, and a third HTTP call
per delivery is not worth a metadata field.

`click_id` is always `None`. No BigCommerce channel carries a Pivota click id
today — the v2 order has no note-attribute/meta field a `clk_*` could ride in
(WooCommerce reuses Order Attribution `utm_content`, Shopify reuses
`note_attributes`), and the writeback stamps none.

Cart item add/remove/update remain Universal Web Collector coverage.

## Official references

- https://docs.bigcommerce.com/docs/integrations/webhooks
- https://docs.bigcommerce.com/docs/integrations/webhooks/events
- https://docs.bigcommerce.com/docs/rest-management/orders/order-status
- https://docs.bigcommerce.com/api-reference/store-management/orders/orders/getanorder
- https://docs.bigcommerce.com/api-reference/store-management/order-transactions/payment-actions/getorderrefunds

The v3 `GET /hooks` **list wrapper and its pagination parameters could not be
verified** — the public reference 404s for the hooks endpoints. The installer
therefore accepts both `{"data": [...]}` and a bare list, pages with
`page`/`limit`, stops as soon as a page comes back short, and caps at 20 pages.

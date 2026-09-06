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
refund notifications. Delivery IDs are retry-stable trace keys on every event
and idempotency keys on the lifecycle ones; refunds are keyed on the money
instead (see below).

## Shoplazza refund amounts

### What the payload carries

Shoplazza has no refund resource in its webhooks. Both refund topics deliver
the **order**, so the receiver sees only what the order says about refunds.

| Field | Status | What it is | How we read it |
| --- | --- | --- | --- |
| `total_refund_price` | VERIFIED, current | "Total refund amount that has been successfully processed", a numeric string. CUMULATIVE across every refund of the order. | The only refund magnitude we read. |
| `currency` | VERIFIED, current | Order currency code. | Required on a refund delivery; a refund without it is rejected 422. |
| `financial_status` | VERIFIED, current | Includes `refunding`, `refund_failed`, `partially_refunded`, `refunded`. | Metadata only. The topic already says a refund settled; the status is a snapshot of the order, not of this refund. |
| `updated_at` / `created_at` | VERIFIED, current | Order timestamps. | `occurred_at`. There is no per-refund timestamp anywhere in the body. |
| `refund_price` | VERIFIED, **deprecated** | "Amount of the most recent refund request (refund events only)." | Never read. It is the requested amount, not a settled one, it carries no identity to dedupe on, and Shoplazza says do not use it. |
| `refund_status` | VERIFIED, **deprecated** | "Refund method of the most recent refund." | Never read. |
| `total_refund_discount`, `total_refund_tax`, `line_items[].refund_quantity`, `line_items[].refund_total` | VERIFIED, **deprecated** | Cumulative refunded discount / tax / per-line quantity and amount. | Never read. |
| `refunds[]` (or any per-refund record array) | VERIFIED **ABSENT** | — | Does not exist. Checked against the `orders/partially_refunded` webhook schema and the 202601 and 202607 Order objects: there is no refund array, no refund record id, and no per-refund amount on the order. |
| Per-refund records with ids (`id`, `refund_price`, `refund_status`, `payment_details[]`) | VERIFIED to exist, but only behind `GET /openapi/2026-01/orders/refund_records` | The Shoplazza refund-record resource. | NOT read. Reaching them means an authenticated API call per delivery inside the platform's 5-second webhook budget; the delta below needs no call. If we ever want per-refund identity and timestamps, that endpoint is where they live. |

ASSUMED, and not verified anywhere: that `total_refund_price` never decreases
in normal operation, and that the platform serialises it consistently across
API versions. Both are handled defensively rather than trusted — a total that
does not exceed what we already recorded emits nothing.

### What we record

Because the only magnitude is cumulative and there is no per-refund identity,
one delivery's refund is `total_refund_price` **minus what this write path has
already recorded for that order**:

* the receiver (`routes/shopline_family_webhooks.py`) reads that figure with
  `services.commerce_interaction_service.recorded_refund_amount_cents`, scoped
  to `(merchant, store, order_ref, write_path='shoplazza_webhook')` and
  excluding synthetic rows, and passes it into the mapper as
  `previously_recorded_refund_cents`. The mapper stays pure;
* `amount_cents` is the delta, and `native_amount_semantics` is
  `cumulative_refund_total_delta`. The cumulative figure itself is preserved as
  `native_cumulative_refund_total`, as before;
* `refund_id` is `<order id>:<cumulative cents>` and the event id is derived
  from it — **not** from the delivery id, which changes between re-fires. Two
  deltas of one order are therefore two distinct keys the funnel sums, while a
  redelivery of the same total is one key the ledger dedupes first-write-wins;
* a delta of zero or less is `ignored` with reason `refund_not_new`, never
  written. The ledger dedupes first-write-wins on the event key, so a
  zero-amount row under `<order>:<total>` would permanently shadow the real
  refund;
* a delivery with **no** `total_refund_price` is `ignored`
  (`refund_total_absent`): nothing claims money moved. A total that is present
  but unreadable or negative is **rejected** 422 — that is a malformed money
  claim, and Shoplazza retries only 5xx, so a 4xx is the loud, non-retried
  answer we want for it. A refund with no `currency` is rejected for the same
  reason: an uncounted amount would still consume the order's key.

### Concurrency

The read and the write are a read-modify-write, so two deliveries for one order
that interleave would both compute their delta against the same stale baseline.
On Postgres both run inside ONE transaction holding
`pg_advisory_xact_lock(hashtext('shoplazza_refund|<merchant>|<store>|<order_ref>'))`
(`order_money_read_modify_write_lock`), so the second delivery reads only after
the first has committed. SQLite has no advisory locks and the helper is a no-op
there; that is tests and local development only. Even unserialised, the
deterministic `<order>:<cumulative>` key means a raced pair collapses into one
row rather than double-counting — the failure mode is understating a refund by
one delta, never inflating it.

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
- https://www.shoplazza.dev/docs/app/building-blocks/webhooks/supported-webhook-events/orders/partially_refunded
- https://www.shoplazza.dev/api/orders
- https://www.shoplazza.dev/api/order-refund-records

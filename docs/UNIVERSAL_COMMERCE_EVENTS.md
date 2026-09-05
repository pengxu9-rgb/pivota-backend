# Universal Commerce Event Ingestion

`POST /merchant-events/v1/batch` is the platform-neutral write contract for store
adapters and Universal Server Collectors. It appends normalized events to the
existing `commerce_interactions` / `commerce_interaction_events` ledger; it is not
a second analytics pipeline. Browser storefronts use the origin-bound public
write path documented in `docs/UNIVERSAL_WEB_COLLECTOR.md`; the browser never
receives the merchant HMAC key.

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

## Shopify native webhook bridge

Verified Shopify `orders/create`, `orders/paid`, `orders/cancelled`, and
`refunds/create` deliveries are dual-written into this contract by
`services/shopify_commerce_event_adapter.py`. The existing append-only Shopify
webhook record and operational order/refund handlers remain authoritative and
unchanged. Canonical ingestion is best effort so analytics availability cannot
change Shopify acknowledgement or retry behavior; a duplicate legacy delivery
still retries the idempotent canonical write for safe rollout backfill.

`refunds/create` emits `refund.created`, but emits `refund.succeeded` only for an
embedded Shopify transaction whose kind is `refund` and status is `success`.
The adapter never treats refund-object creation alone as proof that funds moved.

## PSP terminal-event bridge

After the existing Stripe handler has verified the webhook signature, resolved
the tenant-scoped Pivota order, checked amount/currency integrity, and completed
its operational finalizer, `services/stripe_commerce_event_adapter.py` maps:

- `payment_intent.amount_capturable_updated` to `payment.authorized`
- `payment_intent.succeeded` to `payment.succeeded`
- an applicable `payment_intent.payment_failed` to `payment.failed`
- `refund.created` to `refund.created`
- a succeeded `refund.updated` to `refund.succeeded`

The bridge scopes each event to the connected store selected by the Pivota order,
so `payment_id + order_id` and `payment_id + order_id + refund_id` close the same
store-level interaction graph as native order webhooks. `amount_cents` carries the
PSP minor-unit integer; `native_amount_semantics=psp_minor_units` makes that
explicit for zero-decimal currencies.

Stripe `charge.refunded.amount_refunded` is cumulative and is therefore never
written as an additional refund amount. The adapter emits only embedded successful
refund objects with their individual refund IDs. Their entity-stable canonical
IDs deduplicate against a later `refund.updated` delivery. Canonical PSP ingestion
is best effort and cannot change payment/refund acknowledgement behavior.

For aggregate refund GMV, the funnel sums partial refunds inside each reporting
authority (PSP or store platform), then uses the largest authority total per
store-scoped order. This prevents a Stripe refund and its Shopify/Cafe24/etc.
mirror—with unrelated native refund IDs—from counting the same money twice while
still preserving multiple legitimate partial refunds.

Stripe is the first native PSP bridge. Other PSP handlers remain operational
sources of truth but do not yet publish terminal facts into this canonical ledger.

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

The key proves the merchant, not the store. Every event is bound to one of the
merchant's active connected stores before it is written, using the same store
set the Stripe PSP bridge resolves against so both authorities land in the
same scope:

- `store_id` must name an active connected store; an unknown or disconnected
  store is refused with 422 and the batch is not written.
- `store_id` may be omitted only when the merchant has exactly one connected
  store, which is then filled in. A merchant with several must say which.
- `platform` is taken from the connected store. An omitted platform (or the
  default `custom`) is filled in; an explicit platform that disagrees with the
  store is refused.
- `surface` may not be `psp`; no merchant is a settlement authority.

An event written under a store id the native webhook never uses would sit in
an interaction scope nothing else reaches, so this binding is a correctness
rule as much as a trust rule.
`visitor_id` is retained for analysis but is not used by itself to merge sessions.

Agent identity received through this merchant-signed endpoint is stored as
`merchant_asserted`; it is not equivalent to a cryptographically verified agent.

## Rate limits, metrics, and logging

Every ledger-writing ingress (the three collector routes and the six native
webhook routes) runs inside one envelope, `services/telemetry_ingress.py`:

| Tier | Key | Default | Env |
| --- | --- | --- | --- |
| browser | connected store (from the verified token) | 600 / min | `TELEMETRY_RATE_LIMIT_BROWSER_RPM` |
| merchant | merchant (after the HMAC is proven) | 1200 / min | `TELEMETRY_RATE_LIMIT_MERCHANT_RPM` |
| platform | connected store (after the platform signature is proven) | 3000 / min | `TELEMETRY_RATE_LIMIT_PLATFORM_RPM` |
| auth failures | client hash, public collector routes only | 60 / min | `TELEMETRY_AUTH_FAILURES_PER_IP_RPM` |

The limit is charged against the authenticated principal, never an unverified
header, so a caller cannot dodge it by rotating store ids. A 429 carries
`Retry-After`. Setting a tier to `0` disables it. The window is Redis-backed
when `REDIS_URL` is set and otherwise a bounded in-process store with the same
fixed-window algorithm; Redis errors fail open on the verdict.

The failure budget applies only to `/merchant-events/*`: repeated 401/403 from
one client trip a 429 before the next signature check. Native platform webhooks
arrive from shared egress addresses, so a single misconfigured store must not
throttle its neighbours; those routes get the per-store limit only.

Prometheus: `commerce_telemetry_requests_total{write_path,result,reason}`
(`result` in accepted, rejected, unauthenticated, rate_limited, error;
`reason` is the status code), `commerce_telemetry_events_total{write_path,outcome}`
(accepted, duplicate, ignored, rejected), and
`commerce_telemetry_request_duration_seconds{write_path}`. Every non-2xx is
logged at warning with the write path, principal, status, and a bounded reason.
Request bodies, signatures, and validation inputs are never logged.

## Trust provenance

Every ledger row carries four columns stamped by the ingress that authenticated
the caller. None of them is read from the event body; `source` and `surface`
remain caller-supplied diagnostics and no longer decide whose money a refund is
or whether a row is a probe.

| Column | Values | Set by |
| --- | --- | --- |
| `write_path` | `merchant_hmac_batch`, `universal_web_collector`, `shopify_web_pixel`, `shopify_webhook`, `cafe24_webhook`, `cafe24_reconciliation`, `woocommerce_webhook`, `shopline_webhook`, `shoplazza_webhook`, `sfcc_cartridge`, `adobe_io_events`, `stripe_webhook` | the route that verified the request |
| `authority` | `observational` (browser), `merchant` (HMAC collector), `platform` (native signed webhook, reconciliation replay), `psp` (Stripe bridge) | derived from `write_path` on the server |
| `agent_identity_confidence` | `browser_observed` < `merchant_asserted` < `platform_asserted` < `verified` | fixed per `write_path`; a mismatched pair is refused |
| `synthetic` | boolean, default false | `true` when the batch says `"synthetic": true` or the event surface is `ops_canary` |

`synthetic` is the only provenance a caller may influence, and only downward:
a synthetic batch is excluded from the caller's own default funnel and nothing
else. No production ingress issues `verified` today; the tier exists in the
core and the pairing table keeps it unissued until an ingress actually
authenticates the agent.

Refund de-duplication in the funnel groups by `authority`: a PSP report and a
store-platform report of the same refund are kept as two authorities and the
larger total wins, so a merchant collector that labels itself
`source="stripe_webhook"` is still counted as `merchant`. Rows written before
migration 213 have no stamp and fall back to the legacy `source`/`surface`
inference.

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

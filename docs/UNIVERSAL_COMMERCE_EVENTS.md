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

Delivery is only as good as the subscription. Two install paths exist and both
must register every topic the adapter maps:

- **OAuth / custom-app installs** register per-merchant webhooks from
  `_SHOPIFY_OAUTH_REQUIRED_WEBHOOK_TOPICS` in `routes/merchant_store_connections.py`
  (the ops sweep in `routes/ops_shopify_integration_routes.py` mirrors the same
  list). `refunds/create` joined that list on 2026-09-04; before that only the
  verify flow registered it, so refunds reached the ledger for some merchants
  and not others.
- **App Store installs (App A)** hold no `write_webhooks` scope and receive only
  the app-owned subscriptions declared in `shopify.app.toml`, delivered to the
  static `/webhooks/shopify/orders` endpoint. That list now carries
  `orders/create`, `orders/paid`, `orders/cancelled`, `refunds/create`, and
  `app/uninstalled`. Editing the file registers nothing by itself: a new app
  version must be published with `shopify app deploy` before Shopify delivers
  the added topics.

`tests/test_shopify_refund_webhook_subscription.py` pins both lists and the
toml against `SUPPORTED_SHOPIFY_TOPICS` in the adapter, so a topic added to the
adapter without a subscription fails the build.

## WooCommerce native refund bridge

WooCommerce publishes no refund webhook topic. A refund — partial or full —
arrives as an `order.updated` delivery whose `refunds[]` array has grown by one
entry, and for a partial refund the order status stays `processing`/`completed`.
`services/woocommerce_event_adapter.py` therefore reads `refunds[]` off the order
payload on every delivery and emits one `refund.succeeded` per native refund id,
in addition to whatever order lifecycle events that same webhook produces. The
event id is keyed on the native refund id, so the entries that reappear on every
later `order.updated` dedupe individually while a new one is accepted. Amounts
are `abs()` of the entry's `total` (WooCommerce writes it negative) in the
order's `currency_minor_unit`; `native_amount_semantics=native_refund_total`
marks them per-refund rather than cumulative. The cumulative `total_refunded` is
never written alongside them. Only when `refunds[]` is absent or carries no
usable id — older wc/v3 payloads — does a `refunded` status fall back to the
legacy single event keyed on `<order_id>:refund` with the cumulative amount and
`native_amount_semantics=cumulative_refund_total`. Refund entries carry no
timestamp in the order payload, so `occurred_at` is the order's modification
time, and `refunds[].reason` is merchant free text that is never copied into
canonical metadata.

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

## Order identity across authorities

One purchase reaches the ledger from several writers, and each of them knows
the order by a different id: the Stripe PSP bridge holds the Pivota `orders`
row and says `ord_1`; the Shopify `orders/paid` webhook says `6600123`; the
agent checkout says `ord_1` again; WooCommerce, Cafe24, SHOPLINE, Adobe and
SFCC each say their own native id. `order_id` is therefore several unrelated
namespaces at once, and the funnel keyed paid amounts and order counts on
`(platform, store_id, order_id)` — which de-duplicates only when the two ids
already match. A Pivota-originated Shopify order paid through Stripe counted
its GMV twice, once under each namespace.

`order_ref` is the canonical answer: **the id of the order in its system of
record, namespaced so two systems can never collide.**

| Namespace | Meaning | Example |
| --- | --- | --- |
| `pivota:` | The order originated in Pivota: agent checkout → `orders` row → Stripe → optional writeback to the store platform. | `pivota:ord_1` |
| `<platform>:` | The order was placed on the storefront; the platform is its system of record. | `shopify:6600123`, `woocommerce:44`, `cafe24:20260904-0000011` |

Format: `^[a-z0-9_]+:[^\s]+$`, at most 160 characters
(`services/commerce_order_ref.py`).

### Who sets it

| Writer | Ref | How it knows |
| --- | --- | --- |
| Stripe PSP bridge (`services/stripe_commerce_event_adapter.py`) | `pivota:<orders.order_id>` | It was resolved against the Pivota order row. |
| Agent checkout (`routes/agent_commerce.py`) | `pivota:<checkout_id>` | `checkout_id` **is** the Pivota order id. |
| Attribution edge (`services/commerce_attribution_service.py`) | `pivota:<order_id>` | Every caller passes a Pivota order id. |
| Shopify adapter | `pivota:<id>` if recognised, else `shopify:<native id>` | The `pivota_order_id` note attribute, else the ingest's `orders.shopify_order_id` lookup. |
| WooCommerce adapter | `pivota:<id>` if recognised, else `woocommerce:<native id>` | The `pivota_order_id` entry in the order's `meta_data`. |
| Cafe24 / SHOPLINE / Shoplazza / Adobe / SFCC adapters | `<platform>:<native id>` | Their webhooks are platform-originated. |

### The marker, and the fallback

Pivota's Shopify order writeback stamps a `pivota_order_id` note attribute on
every order it creates (it previously did so only when the order also carried
discount annotations, so a plain order had no marker at all). The WooCommerce
writeback stamps the same key into the order's `meta_data`. A later webhook for
that order therefore recognises it as Pivota-originated from its own body, with
no lookup.

For Shopify orders written back *before* the marker existed,
`services/shopify_commerce_event_ingest.py` falls back to a single indexed
lookup on the unique `orders.shopify_order_id` column and passes the Pivota
order id into the adapter. WooCommerce has no equivalent indexed column — the
writeback records the native id inside `orders.metadata` — so pre-marker
WooCommerce orders keep their `woocommerce:` identity.

### What it changes, and what it does not

- **Stitching.** `order_ref` is a strong, store-scoped lookup key in
  `services/commerce_interaction_service.py`, with a unique index
  `(merchant_id, COALESCE(store_id, ''), order_ref)` mirroring the `order_id`
  one. A Stripe `payment.succeeded` and a Shopify `orders/paid` for the same
  purchase converge on ONE interaction even when no click id ties them
  together. It is store-scoped because `_merge_interactions` refuses a
  cross-store merge; the two authorities that share a canonical order also
  share a store scope, because both resolve the merchant's connected store.
- **Funnel.** `order_ref` rows are keyed in one global scope rather than
  `(platform, store_id)`; the existing `max` across `payment.succeeded` and
  `order.paid` per key is then what removes the double count. Refunds and
  payments that carry only a `payment_id` inherit the order's ref through the
  same resolution maps.
- **Legacy rows.** `order_ref IS NULL` on every row written before migration
  216, and those rows keep aggregating on `(platform, store_id, order_id)`
  exactly as before. `order_id` itself is untouched and remains the diagnostic
  record of what each authority called the order.

### Who may claim one

`order_ref` is server-set. A merchant HMAC collector *is* its own platform's
server for its own orders, so it may send one — but only in its bound store's
platform namespace (`services/merchant_event_store_binding.py`). A `pivota:`
ref from a merchant collector is refused with 422: `order_ref` is a strong
stitch key, so a forged one would merge the collector's events into a
Pivota-originated interaction it does not own. Browser collectors may not send
`order_ref` at all — it joins `order_id` in
`FORBIDDEN_WEB_EVENT_FIELDS`.

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
| `write_path` | batch ingress: `merchant_hmac_batch`, `universal_web_collector`, `shopify_web_pixel`, `shopify_webhook`, `cafe24_webhook`, `cafe24_reconciliation`, `woocommerce_webhook`, `shopline_webhook`, `shoplazza_webhook`, `sfcc_cartridge`, `adobe_io_events`, `stripe_webhook`; first-party writers: `agent_commerce_api`, `surface_click_attribution`, `commerce_attribution_edge`, `surface_listing_registry` | the route or service that verified the request |
| `authority` | `observational` (browser), `merchant` (HMAC collector), `platform` (native signed webhook, reconciliation replay), `psp` (Stripe bridge), `pivota` (first-party checkout, attribution, and listing facts) | derived from `write_path` on the server |
| `agent_identity_confidence` | `unknown` < `browser_observed` < `merchant_asserted` < `platform_asserted` < `verified` | fixed per `write_path`; a mismatched pair is refused |
| `synthetic` | boolean, default false | `true` when the batch says `"synthetic": true` or the event surface is `ops_canary` |

`synthetic` is the only provenance a caller may influence, and only downward:
a synthetic batch is excluded from the caller's own default funnel and nothing
else.

`verified` is issued by exactly one write path, `agent_commerce_api`
(`routes/agent_commerce.py`). Every branch of its `get_agent_context`
dependency authenticates the agent's own Pivota credential before yielding an
agent id: an API key looked up in the agents table, a Pivota-signed checkout
token whose agent id is then looked up, or an internal trusted key. `verified`
therefore means "the writing ingress authenticated the credential Pivota issued
to this agent". It is not a statement about the agent vendor beyond that
credential. The click, attribution-edge, and listing writers carry whatever
agent id their attribution context held and may only assert `unknown`.

The vocabulary and pairing table live in
`services/commerce_ledger_provenance.py`; direct writers splat
`ledger_provenance(write_path, confidence)` so the authority is never typed by
hand, and `record_commerce_event` refuses a caller that names a different one.

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

Every canonical read is bounded by a TIME WINDOW as well as by a row limit.

```text
GET /merchant/analytics/commerce-funnel?since=2026-06-01T00:00:00Z&until=2026-06-30T23:59:59Z
```

`since` and `until` are ISO-8601 and inclusive; a naive value is read as UTC,
never as process-local. Omitting both reads the last
`COMMERCE_FUNNEL_DEFAULT_WINDOW_DAYS` days (default 90); omitting only `until`
ends the window at now. A caller may widen up to
`COMMERCE_FUNNEL_MAX_WINDOW_DAYS` (default 400); a wider span is clamped to it.
`since` after `until` is a 422, as is an unparseable bound. The response
carries the slice it actually aggregated:

```json
"window": {"since": "2026-06-01T00:00:00+00:00", "until": "2026-06-30T23:59:59+00:00", "days": 30, "clamped": false}
```

WHY. Without a window the row limit alone decided the population, and it cuts
on recency, not on purchases: a `refund.succeeded` inside the newest N can
belong to an `order.paid` that fell outside it, so `paid_amount_cents_by_currency`
and `refunded_amount_cents_by_currency` stopped describing the same set of
purchases with nothing in the response saying so. The bounds are applied in
SQL, on `occurred_at`, served by migration 206's
`idx_commerce_interaction_events_merchant_occurred` — verified by `EXPLAIN` on
real Postgres in `tests/test_commerce_ledger_retention_postgres.py`, so no new
index was added.

The window bounds the CANONICAL ledger only. The legacy listing, click and
attribution rows stay all-time and report
`metric_scopes.legacy_attribution.time_windowed=false`: `surface_click_events`
and `commerce_attribution_edges` rows are mutable accumulators (impressions,
clicks, refund counts and amounts are incremented on an existing row), so their
`created_at` is the row's birth rather than the time of the activity they
count, and bounding on it would drop today's click on a year-old row. Listing
rows carry no event time at all.

Reads are also newest-first and bounded by `COMMERCE_FUNNEL_LEDGER_EVENT_LIMIT`
(default 50,000, minimum 100, maximum 200,000). The response sets
`event_funnel.truncated=true` when the bound was reached, so callers never
mistake a partial aggregate for a complete all-time count. `truncated=true`
inside a wide window is the signal to narrow the window rather than to widen
the limit.
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

## Retention

The ops telemetry canary writes an eight-event chain per run and, until now,
nothing ever deleted one: neither `commerce_interaction_events` nor
`commerce_interactions` had any cleanup at all. `scripts/sweep_commerce_ledger_synthetic.py`
deletes aged PROBE rows only — `synthetic IS TRUE` (migration 213's column,
served by migration 214's partial index) plus the pre-column shape
`surface = 'ops_canary'`, which this sweep deliberately treats as synthetic so
that probe rows written before 213 are not permanently undeletable. It is a
DRY RUN by default and prints its JSON result; `--apply` performs the deletes,
in batches of `--batch-size` with one transaction per batch, bounded by
`--max-batches`. An interaction is deleted only when no `commerce_interaction_events`
row of any kind still points at it, so an interaction carrying one real event
and one synthetic event keeps both its row and its real event. As a Cloud Run
job, following the convention in `docs/runbooks/derive_offer_market_currency.md`
(`gcloud run jobs execute --args` overrides the Job's baked args, so the full
argument list is given each time):

```bash
gcloud run jobs execute sweep-commerce-ledger-synthetic --region us-west1 --project pivota-prod --wait \
  --args='-m,scripts.sweep_commerce_ledger_synthetic,--older-than-days,7,--apply'
```

Recommended cadence is daily at a low-traffic hour with `--older-than-days 7`,
which keeps a week of canary chains available for debugging a failed run.
**Scheduling is ops config, not code** — this PR ships the script and no
schedule; creating the Job and its Cloud Scheduler trigger is a separate,
deliberate ops step, and CI never updates Cloud Run Jobs.

### Real commerce history

No real row is deleted anywhere in this lane, and there is no retention policy
for real history yet. `--report-horizon-days N` measures what such a policy
would cost: events and interactions older than N days, per merchant, with the
oldest `occurred_at` on file. Two options once the numbers are in:

* **Native range partitioning on `occurred_at`.** Dropping a partition is
  instant and never bloats the table. Cost: `commerce_interaction_events` is an
  established table, so this is a full rewrite — a new partitioned table, a
  backfill, a cutover of every writer, and re-creating six indexes.
* **Periodic archive to GCS, then delete.** No schema change and history stays
  readable offline. Cost: batched `DELETE`s leave dead tuples for autovacuum,
  the archive becomes a second source of truth that no query joins, and each
  run must be idempotent against a partly-finished previous one.

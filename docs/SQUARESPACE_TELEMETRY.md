# Squarespace commerce telemetry

Squarespace is the first platform in this repo where **the telemetry path a
merchant gets depends on which credential they connected**, and that single fact
shapes everything below.

| Credential | Reaches | Telemetry |
| --- | --- | --- |
| Per-site **Developer API key** (Settings → Developer API Keys) | Orders API | reconciliation **sweep only** |
| **OAuth access token** from a Squarespace Developer Platform app | Orders API **and** Webhook Subscriptions API | webhook **and** sweep |

Webhook subscriptions are an OAuth surface. A per-site API key cannot create
one, so `POST /integrations/squarespace/{store_id}/webhooks/ensure` answers
**409 `oauth_required`** for an API-key store and points at the sweep, rather
than reporting a provisioning that cannot exist. See *Verified vs assumed*
below — this is the claim the whole design rests on.

| Piece | Where |
| --- | --- |
| Connection | `POST /integrations/squarespace/connect` (`routes/merchant_store_connections.py`) |
| Receiver | `POST /webhooks/squarespace/{store_id}` (`routes/squarespace_webhooks.py`) |
| Order read-back | `services/squarespace_order_fetch.py` |
| Mapper (pure) | `services/squarespace_event_adapter.py` |
| The one ingest path | `services/squarespace_ledger.py` |
| Sweep | `services/squarespace_order_sweep.py` |
| Sweep, as a route | `POST /integrations/squarespace/{store_id}/reconcile` |
| Sweep, as a script | `scripts/sweep_squarespace_orders.py` |
| Subscription provisioning | `POST /integrations/squarespace/{store_id}/webhooks/ensure`, `services/squarespace_webhook_subscriptions.py` |
| Credential shape / persistence | `services/squarespace_connection.py` |

No schema change. The credential blob lives in `merchant_stores.api_key` as
JSON, exactly like BigCommerce and PrestaShop:

```json
{"api_key": "...", "website_id": "...", "oauth_access_token": "...",
 "webhook_secret": "...", "webhook_subscription_id": "...",
 "reconciliation": {"orders_cursor": "2026-09-05T11:30:00.000Z",
                    "last_run_at": "...", "overlap_minutes": 30}}
```

## Connect

```
POST /integrations/squarespace/connect
{"merchant_id": "...", "api_key": "...", "oauth_access_token": "...?",
 "store_name": "...?", "domain": "...?"}
```

The credential is validated by calling `GET /1.0/authorization/website`, which
is also the **binding** step: the `id` it returns is persisted as `website_id`,
and every webhook delivery must name it. Without that binding, a notification
signed with some *other* Squarespace site's subscription secret could not be
distinguished from this store's own — the subscription secret belongs to a
subscription, not to a site.

When both credentials are supplied the OAuth token is the one validated and the
one used for reads: it is the identity that also carries webhook subscriptions,
so it is the identity worth proving.

**A reconnect read-modify-writes the blob.** The same cell holds the webhook
secret (Pivota's only copy) and the reconciliation cursor. Overwriting it is
verbatim the PrestaShop P1: the receiver then 401s every delivery and the
merchant has no way to tell a rotated secret from an outage. The one case where
preserving is wrong is a credential that now belongs to a **different** site —
then `webhook_secret`, `webhook_subscription_id` and `reconciliation` are
dropped, because the secret would authenticate deliveries the `websiteId` bind
rejects and the cursor is a high-water mark over another site's orders.

The key is never logged. The connect log line carries the store, merchant,
website id, and whether an OAuth token was supplied — nothing more.

## The wire

```
POST /webhooks/squarespace/{store_id}
Squarespace-Signature: <hex HMAC-SHA256 of the raw body, keyed with the subscription secret>

{"id": "...", "topic": "order.update", "createdOn": "...",
 "websiteId": "...", "subscriptionId": "...", "data": {"orderId": "..."}}
```

Auth chain, in order:

1. 1 MB body cap → 413;
2. an **active** `platform = 'squarespace'` store row for `{store_id}`;
3. a `webhook_secret` in that row's credential JSON;
4. constant-time HMAC-SHA256 over the raw body (hex **or** base64 — see the
   assumed table);
5. `identify` + the `platform` rate-limit tier;
6. the JSON parses and is an object → 400;
7. the body's `websiteId` equals the store's bound `website_id`;
8. the topic is `order.create` or `order.update`;
9. the notification `id` has not already been ingested by this process;
10. `data.orderId` is present → 422;
11. the order is fetched → 503 on failure;
12. map, and ingest with `write_path="squarespace_webhook"` /
    `agent_identity_confidence="platform_asserted"` → authority `platform`.

Steps 2–4 answer with one 401 message, so a caller never learns which it hit —
including an API-key-only store, whose "no secret" state is indistinguishable
from a wrong signature. Step 7 has its own 401 message: it is a configuration
error on a delivery that already proved it holds a secret.

**A malformed signature header is 401, never 500.** `hmac.compare_digest` raises
`TypeError` on a str with non-ASCII code points, and Starlette decodes header
values as latin-1, so a hostile header of high bytes would otherwise be a
500 — a denial-of-service handle rather than a refusal.

**The delivery is thin.** It names an order and carries no order fields, so the
order is read back before anything is mapped, and a failed read is answered
**503**: Squarespace retries a non-2xx, and a 200 would drop the event until the
sweep's next window.

**The notification dedupe is an optimisation, not a guarantee.** A bounded
per-process LRU of notification ids saves a redundant Orders API call on a
redelivery, and ids are recorded only *after* a successful ingest so a delivery
that 503'd is retried rather than swallowed. The actual correctness guarantee is
the ledger's deterministic event ids, which hold across processes, restarts, and
the sweep.

## Mapping

| Canonical event | When | `occurred_at` | `amount_cents` |
| --- | --- | --- | --- |
| `order.created` | always | `createdOn` | `grandTotal` |
| `order.paid` | `grandTotal > 0` and a currency is known | `createdOn` | `grandTotal` (`native_amount_semantics=order_grand_total`) |
| `order.cancelled` | `fulfillmentStatus == CANCELED` | `modifiedOn` | `grandTotal` |
| `refund.succeeded` | `refundedTotal` exceeds what Pivota already recorded | `modifiedOn` | the **delta** (`native_amount_semantics=cumulative_refund_total_delta`) |

**`order.paid` is emitted from the order's existence.** The Orders API carries
no payment status of any kind; an order exists there because a checkout was
paid. It is anchored at `createdOn`, not `modifiedOn`, because the payment is
what *caused* the order — an order edited weeks later must not move its own
payment forward with it.

**`testmode` orders are ignored entirely**, never mapped and never counted. A
test order was not paid for; recording even `order.created` for it would occupy
that order's deterministic keys and put fabricated GMV in the funnel.

`order_ref` is `squarespace:<order id>`. `orderNumber` is kept as
`native_order_number` metadata. Line items keep join keys and the unit amount
only — `productName` is merchant copy and `customizations` is buyer free text,
so neither reaches the ledger. `customerEmail` is the only buyer field
Squarespace exposes and is PII, so `buyer_id` is always `None`.

`externalOrderReference` is **never** read as a Pivota order identity. It is
free text an extension can set, and reading it would let a forged `pivota:`
string merge an order into an interaction it does not own — the same reasoning
as the BigCommerce adapter. Recovering a Pivota identity from a Squarespace
order would need a structured marker stamped at writeback, and no Squarespace
order writeback exists in this repo.

**Event ids are derived from the order id and the event type** (plus the refund
key), never from the notification id. That is what makes a webhook observation
and a later sweep observation of the same order collapse onto one ledger row
instead of counting every purchase twice for an OAuth store, which has both
paths armed. `tests/test_squarespace_ledger_end_to_end.py` and the Postgres gate
both pin it.

## Refunds are a cumulative delta

`refundedTotal` is one money object on the order. There is no `refunds[]`
array, no refund id, and no per-refund timestamp anywhere in the Orders API. So
the only per-observation refund amount that exists is the delta against what
Pivota already recorded — the Shoplazza shape, and deliberately the same code:

* the ingress reads
  `services.commerce_interaction_service.recorded_refund_amount_cents` and hands
  the figure to the pure mapper as `previously_recorded_refund_cents`;
* the mapper emits `amount_cents = cumulative - previously` under
  `refund_id = <order id>:<cumulative cents>`, with the event id derived from
  that key;
* a delta of zero or less emits **nothing**. The ledger is first-write-wins on
  the key, so a zero-amount row under `<order>:<total>` would permanently shadow
  the real refund that arrives under the same key later.

Two things are Squarespace-specific and both matter.

**The baseline read spans BOTH write paths.** `recorded_refund_amount_cents` now
accepts a sequence, and the Squarespace callers pass
`("squarespace_webhook", "squarespace_reconciliation")`. Scoping it to the
caller's own path would make the sweep read a baseline of 0 for an order the
webhook already recorded, and emit the whole cumulative total under a second
`<order>:<cumulative>` key — which the funnel SUMS. A webhook that recorded 1000
followed by a sweep seeing 2500 would produce 1000 + 2500 = 3500 against a true
cumulative of 2500. Shoplazza's single-path behaviour and its tests are
unchanged: a bare string still compiles to `write_path = $n`, and only a
sequence compiles to `IN (...)`.

**The lock is required, not an optimisation.** The read and the write are a
read-modify-write; on Postgres they run inside one transaction holding
`pg_advisory_xact_lock` on the order key (`order_money_read_modify_write_lock`,
scope `squarespace_refund`). The deterministic key only collapses observations
carrying the SAME cumulative total; a raced pair carrying different totals both
read the same baseline and emit two distinct keys the funnel sums — an
INFLATION of refunded GMV, not an understatement. The helper is a no-op on
SQLite, which is tolerable only because SQLite is tests and local development.
The lock is taken only when the order actually reports refunded money: with
`refundedTotal` at zero there is no delta whatever the baseline says, and an
advisory lock per `order.create` would serialise a store's whole delivery
stream.

ASSUMED, and handled defensively rather than trusted: that `refundedTotal` never
decreases. After a downward correction (25.00 corrected to 20.00, which is
ignored) the next genuine refund to 30.00 emits a delta of 5.00 rather than
10.00; the running total still lands on 30.00, so aggregate refunded GMV is
right and only that one per-event delta is short.

## The sweep

```
POST /integrations/squarespace/{store_id}/reconcile?apply=true&max_pages=20
python -m scripts.sweep_squarespace_orders --store-id store_x --apply
```

One store per run. `GET /1.0/commerce/orders?modifiedAfter=&modifiedBefore=` for
the first page, then `?cursor=` for each later one — the bounds and the cursor
are mutually exclusive. Every order runs through the SAME mapper and the same
locked refund arithmetic as the receiver, with
`write_path="squarespace_reconciliation"`.

Cursor safety, all three rules:

* the window starts at **`cursor − overlap`** (default 30 minutes), so an order
  modified in the same instant the last run ended is re-read rather than
  skipped. Re-reading is free: the event ids are deterministic and the ledger
  dedupes;
* the cursor **never moves backwards**, so a back-dated edit or a clock skew
  cannot re-open a window that was already closed;
* the cursor is **not advanced when the page cap truncated the run**.
  Squarespace documents no ordering for the orders list, so a run stopped early
  may have left behind orders whose `modifiedOn` is below the maximum it saw;
  advancing past them would lose them for good. A truncated run reports
  `truncated: true` and the next run re-reads the same window. Raise
  `--max-pages` (or narrow the window) if a store stays truncated.

An empty but *complete* window advances the cursor to the window's end: nothing
modified in a window that was fully read means nothing was missed in it, and
leaving the cursor put would make the window grow without bound over a quiet
period. However stale a cursor is, one run never asks for more than 90 days.

`--apply` is required to write anything; the default is a dry run that lists and
classifies and leaves the cursor alone. A run that fails mid-window persists no
cursor at all.

## Scheduling — the gap

**Nothing schedules this sweep automatically today, and that is deliberate
rather than an oversight.** Two facts from this repo:

* CI deploys **no Cloud Run job** for this lane. `deploy-prod` does not build or
  ship one, so adding a job entry here would be a scheduled run that never
  exists.
* The APScheduler lane (`services/audit_scheduler.py`, where
  `cafe24_reconciliation` lives) runs inside the `worker` service, and the
  Cafe24 tick has been dormant behind `CAFE24_RECONCILIATION_ENABLED` since it
  was written. Registering a Squarespace tick there would inherit the same
  unproven lane.

So the sweep ships with **two reachable, authenticated surfaces and no
scheduler**: the `reconcile` route (merchant-or-admin, ownership checked from
the row) and the script. Until a scheduled run exists, an API-key store's
telemetry latency is the interval at which one of those is invoked. Wiring it up
is a follow-up that should register the job in `services/audit_scheduler.py`
alongside `cafe24_reconciliation`, add its id to `_RUNNABLE_JOB_IDS` in
`routes/admin_scheduler_jobs.py`, and — separately — make the lane actually
deploy.

## Provisioning the subscription

```
POST /integrations/squarespace/{store_id}/webhooks/ensure
```

Merchant-or-staff role gate; ownership comes off the fetched ROW, because
`store_id` is caller-supplied and the SELECT keys on it alone.

* **API key only → 409 `oauth_required`**, with the store's `reconcile_path` in
  the detail. Nothing is written.
* **No `website_id` → 409.** Deliveries could not be bound to the site they came
  from; the store has to be reconnected.
* **OAuth token → create.** Every subscription already pointing at our endpoint
  is deleted first and a fresh one created. This is not tidiness: the secret is
  returned by Squarespace exactly ONCE, at creation, and cannot be read back, so
  reusing a subscription whose secret Pivota lost would leave it delivering
  notifications the receiver can only answer 401 to.

The secret is persisted, then the row is **re-read** — `databases` + asyncpg
reports no rowcount from an `UPDATE`, so the re-read is the only proof the write
landed, and under a race it is the only way to learn which writer won. If the
persisted secret is not this request's, a concurrent ensure won: this request's
subscription is deleted and the winner's state is reported. If nothing persisted
at all, the subscription is deleted and the call answers 503.

**The secret is never returned and never logged.** Pivota installs the
subscription itself, so unlike PrestaShop no human ever needs to see it; the
response says `secret_provisioned: true` and which subscription and topics were
installed. `extension.uninstall` is subscribed so the event is observable in
Squarespace's own subscription list; the receiver ignores it, because
disconnecting a store is a merchant-facing decision rather than a receiver's.

The callback origin comes from `SQUARESPACE_WEBHOOK_BASE_URL`, `PUBLIC_BASE_URL`
or `PIVOTA_BACKEND_BASE_URL` and must be HTTPS with no credentials, query or
fragment, or the call is 503.

## Verified vs assumed

"Verified" means checked against the Squarespace Commerce API documentation and
the platform's own behaviour as of 2026-09-06. "Assumed" means the code depends
on it and it is **not** confirmed — each row says what happens if the assumption
is wrong.

| # | Claim | Status | If wrong |
| --- | --- | --- | --- |
| 1 | Base URL `https://api.squarespace.com/1.0/`, auth `Authorization: Bearer <key>` | **Verified** | — |
| 2 | A `User-Agent` header is **required**; Squarespace answers 400 without one | **Verified** | Every call 400s; caught immediately at connect |
| 3 | 429 on rate limit | **Verified** | Surfaces as a retryable fetch/sweep error either way |
| 4 | `GET /1.0/authorization/website` returns the site the credential belongs to, with `id` and `title` | **Verified** | Connect fails closed: no `website_id`, no store row |
| 5 | The authorization response is the website object itself (not wrapped) | **Assumed** | A `{"website": {...}}` envelope is also accepted; anything else fails connect loudly |
| 6 | `GET /1.0/commerce/orders` accepts `modifiedAfter` + `modifiedBefore` and paginates via `pagination.nextPageCursor` | **Verified** | Sweep errors on the first page; no silent partial run |
| 7 | `modifiedAfter`/`modifiedBefore` must be sent **together**, and `cursor` may not be sent with them | **Assumed** | If they were compatible, sending only the cursor on later pages is still correct — this is the conservative direction |
| 8 | Order fields `id`, `orderNumber`, `createdOn`, `modifiedOn`, `testmode`, `fulfillmentStatus`, `grandTotal`, `refundedTotal`, `lineItems[]`, `externalOrderReference` | **Verified** | A missing money field yields no money event rather than a zero one |
| 9 | `fulfillmentStatus ∈ {PENDING, FULFILLED, CANCELED}` | **Verified** | An unknown value simply emits no `order.cancelled` |
| 10 | Money is `{"value": "10.00", "currency": "USD"}` | **Verified** | Unreadable amounts yield no money event |
| 11 | The Orders API has **no** payment status, and an order exists only after a successful payment (except `testmode`) | **Verified** (field absence) / **Assumed** (the inference) | If unpaid orders can exist, `order.paid` overstates GMV for those orders. The `testmode` exclusion covers the documented case |
| 12 | `refundedTotal` is **cumulative** across every refund of the order | **Assumed** (it is a single field with no per-refund records, which forces the reading) | If it were per-refund, every refund after the first would be recorded as a delta and under-count. The Postgres gate and the e2e suite pin the cumulative arithmetic so a correction is a one-place change |
| 13 | `refundedTotal` never decreases | **Assumed** | Handled: a decrease records nothing, and the next genuine refund's delta is short by the correction. Aggregate refunded GMV stays right |
| 14 | Webhook Subscriptions API `POST/GET/DELETE /1.0/webhook_subscriptions`, topics `order.create`, `order.update`, `extension.uninstall` | **Verified** | `ensure` fails 502 with the platform's status; nothing is persisted |
| 15 | **Webhook subscriptions are OAuth-only; a per-site API key cannot create one** | **Assumed (high confidence)** | This is the load-bearing assumption. If an API key *can* subscribe, the 409 `oauth_required` is over-strict: those stores get sweep latency instead of push, and the fix is to stop requiring `oauth_access_token` in `ensure_squarespace_webhooks`. Nothing is mis-recorded either way |
| 16 | The subscription `secret` is returned exactly once, at creation, and cannot be read back | **Assumed** | If it can be read back, deleting-and-recreating an existing subscription is unnecessary churn, not a correctness problem |
| 17 | Notifications are POSTed with `Squarespace-Signature` = HMAC-SHA256 over the raw body with the subscription secret | **Verified** (mechanism) | — |
| 18 | That signature is **hex**-encoded | **Assumed** | Hedged: base64 is accepted too, both under `hmac.compare_digest`. Accepting both costs nothing — a caller must still produce the digest |
| 19 | Notification body `{id, topic, createdOn, websiteId, subscriptionId, data: {orderId}}`, thin | **Verified** | A body with no `data.orderId` is 422 and nothing is recorded |
| 20 | There is no rotate-secret endpoint | **Assumed** | Only affects `ensure`'s delete-and-recreate strategy |

## Residual gaps

* **API-key sites get sweep latency, not push.** With no scheduler in this repo
  for the lane (above), that latency is however often the route or script is
  invoked.
* **No PSP visibility.** `refundedTotal` is Squarespace's own figure. Pivota sees
  no payment-processor refund record for a Squarespace order, so a refund issued
  outside Squarespace's own commerce flow is invisible to this bridge.
* **No refund identity.** Two partial refunds of the same order in the same
  cumulative total (i.e. observed only once, after both) arrive as one delta.
  The total is right; the count is one, not two.
* **No Pivota order identity.** Nothing links a Squarespace order back to a
  Pivota-originated purchase, so every Squarespace order is platform-originated
  in the ledger. Closing that needs a structured marker at writeback, and there
  is no Squarespace order writeback.
* **No catalogue.** `services/commerce_source_registry.py` claims no catalogue
  capability for Squarespace; a product sync would report an honest blocker
  rather than an empty success.
* **No `extension.uninstall` handling.** The topic is subscribed and the
  notification is acknowledged, but nothing deactivates the store; a merchant
  who uninstalls the app leaves an active store row whose sweep will start
  failing on 401.
* **`order.paid` is inferred, not observed.** See assumption 11.

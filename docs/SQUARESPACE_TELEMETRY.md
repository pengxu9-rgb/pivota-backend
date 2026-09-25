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
 "oauth_refresh_token": "...?", "oauth_expires_at": "...?",
 "store_name": "...?", "domain": "...?"}
```

`oauth_refresh_token` and `oauth_expires_at` are **persisted but not yet used**
— there is no refresh path (assumption 21, Residual gaps). Storing them now
means the refresh, when it lands, does not require every OAuth store to
reconnect first.

The credential is validated by calling `GET /1.0/authorization/website`, which
is also the **binding** step: the `id` it returns is persisted as `website_id`,
and every webhook delivery must name it. Without that binding, a notification
signed with some *other* Squarespace site's subscription secret could not be
distinguished from this store's own — the subscription secret belongs to a
subscription, not to a site.

When both credentials are supplied the OAuth token is the one validated and the
one **preferred** for reads: it is the identity that also carries webhook
subscriptions, so it is the identity worth proving. Preferred, not required —
reads fall back to the API key on a 401, because the OAuth token expires and the
API key does not (assumption 21).

A connect that fails names the **upstream status** in its 400 detail
(`… (upstream HTTP 404)`). Assumption 4 — that `authorization/website` answers a
per-site API key at all — is load-bearing and unverified; a bare "connection
failed" would look exactly like a mistyped key.

**A reconnect read-modify-writes the blob, inside ONE critical section.** The
same cell holds the API key, the OAuth token, the website binding, the webhook
secret (Pivota's only copy) and the reconciliation cursor. Overwriting it is
verbatim the PrestaShop P1: the receiver then 401s every delivery and the
merchant has no way to tell a rotated secret from an outage. Connect therefore
goes through `merge_squarespace_credentials` like every other writer, passing a
`mutate` callback that runs **under the same row lock** as the read and the
write — a second, hand-rolled read-modify-write beside the shared one meant a
sweep's cursor write landing between connect's read and its write reverted the
merchant's new credential.

The one case where preserving is wrong is a credential that now belongs to a
**different** site. Then `webhook_secret`, `webhook_subscription_id`,
`reconciliation` **and every OAuth field** (`oauth_access_token`,
`oauth_refresh_token`, `oauth_expires_at`) are dropped. The OAuth token matters
most and is the easiest to miss: it is *preferred over the API key on every
read*, so a token the old site issued keeps the sweep listing the OLD site's
orders and recording them under the store that now represents the new one. A
token supplied *with* the reconnect is kept — the drop is of the stale identity,
not a downgrade to sweep-only.

`telemetry_mode` in the response is read off the blob **that persisted**, not
off the request field: a reconnect that supplies no OAuth token still has
webhooks if the stored one survived, and answering `sweep_only` there would tell
the merchant their armed subscription is not armed.

The key is never logged. The connect log line carries the store, merchant,
website id, and whether an OAuth token was supplied — nothing more.

### The credential blob is written under a row lock

`merge_squarespace_credentials` is the ONE writer of `merchant_stores.api_key`
for this platform, and it runs read → mutate → write → re-read inside
`database.transaction()` behind `SELECT … FOR UPDATE` (on Postgres; a plain
select on SQLite, which has no `FOR UPDATE` and where this is tests and local
development only).

The lock is not defensive habit. Without it, two writers both read the pre-write
blob and the second silently discards the first: a sweep persisting its cursor
between `ensure`'s read and its write **erases the `webhook_secret`** — after
which every delivery 401s and no reconnect can recover it, because Squarespace
shows that secret exactly once — and the reverse interleaving reverts a
reconnect to the credential the merchant just replaced. Both interleavings are
reproducible. The serialization claim is pinned in
`tests/test_squarespace_ledger_postgres.py` with two genuinely separate
backends, because SQLite cannot observe it.

The re-read is not belt-and-braces either: `databases` + asyncpg reports no
rowcount from an `UPDATE`, so reading the row back is the only proof the write
landed.

## The wire

```
POST /webhooks/squarespace/{store_id}
Squarespace-Signature: <hex HMAC-SHA256 of the raw body, keyed with the subscription secret>

{"id": "...", "topic": "order.update", "createdOn": "...",
 "websiteId": "...", "subscriptionId": "...", "data": {"orderId": "..."}}
```

Auth chain, in order:

1. 1 MB body cap → 413, enforced **while the body streams in** (and from
   `Content-Length` first), not after buffering it — same shape as
   `routes/prestashop_webhooks.py`. Measuring after buffering means a hostile
   sender has already made this process hold whatever they sent;
2. an **active** `platform = 'squarespace'` store row for `{store_id}`;
3. a `webhook_secret` in that row's credential JSON;
4. constant-time HMAC-SHA256 over the raw body (hex either case, standard or
   url-safe base64, padded or not — see assumptions 17 and 18);
5. `identify` + the `platform` rate-limit tier;
6. the JSON parses and is an object → 400;
7. the body's `websiteId` equals the store's bound `website_id`;
8. the topic is `order.create` or `order.update`;
9. the notification `id` has not already been ingested by this process;
10. `data.orderId` is present → 422;
11. the order is fetched — the OAuth token first, falling back to the API key on
    a 401/403 only (a 429 or 5xx is not retried with a second identity: the
    credential was fine and the API was not) → 503 when no credential works;
12. map, and ingest with `write_path="squarespace_webhook"` /
    `agent_identity_confidence="platform_asserted"` → authority `platform`.

Steps 2–4 answer with one 401 message, so a caller never learns which it hit —
including an API-key-only store, whose "no secret" state is indistinguishable
from a wrong signature. Step 7 has its own 401 message: it is a configuration
error on a delivery that already proved it holds a secret.

**A rejected delivery logs what makes a wrong assumption diagnosable.** At
WARNING: the **names** of the `Squarespace-*` headers present (never a value),
whether the store was known, whether a secret was stored, and whether the digest
arrived hex- or base64-shaped. Assumption 17 — that the signed input is the raw
body *alone* — is unverified; if it is wrong, the symptom is a 401 on every
delivery, which is indistinguishable from a wrong secret without this line. A
`Squarespace-Timestamp` in that header list, on a store whose secret is known
good, is the tell.

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
that 503'd is retried rather than swallowed. A short-circuited redelivery
answers `duplicates: 1`, not 0 — it IS a duplicate observation, and the ledger
would have counted it as one had the cache not saved the API call; reporting
zero would make the metric read as if redeliveries never happen. The actual
correctness guarantee is the ledger's deterministic event ids, which hold across
processes, restarts, and the sweep.

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
POST /integrations/squarespace/{store_id}/reconcile?modified_before=2026-02-01T00:00:00Z
python -m scripts.sweep_squarespace_orders --store-id store_x --apply
python -m scripts.sweep_squarespace_orders --store-id store_x \
    --modified-before 2026-02-01T00:00:00Z --apply
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
  advancing past them would lose them for good.

### Truncation is bisected, not frozen

Holding the cursor on truncation is only half an answer, and on its own it is a
**trap**. The window is `cursor − overlap → now`. If the cursor is held while
`now` keeps advancing, every subsequent run asks for a *wider* window than the
one that already failed, truncates on the same page-cap prefix, and the rest of
the range is never read — permanently. A moderately busy store falls into that
on its very first sweep (7 days × 20 pages) and never climbs out; the symptom is
a store that reports `truncated: true` forever and whose cursor is `null`.

So the sweep **bisects**:

* a truncated run records the end of the window it could not finish
  (`truncated_window_end`) in the reconciliation state;
* the next run halves towards it —
  `modifiedBefore = window_start + (window_end − window_start) / 2` — and keeps
  halving until a window fits under the page cap;
* a bounded window that **completes** advances the cursor to *that window's end*
  (not to the highest `modifiedOn` seen, which would leave the next window
  overlapping the prefix just finished) and **doubles** the width it tries next,
  so a store that fell behind climbs back to real time geometrically;
* the narrowest window is `overlap + 5 minutes`, never less. A window narrower
  than the overlap would advance the cursor by less than the following window
  rewinds it, and the sweep would go backwards;
* if even that narrowest window still truncates, the run **accepts it, advances
  past it, and logs an ERROR naming the exact range** that may be short. Staying
  put would be an unbounded outage; a silent skip would be worse than a loud one.

`modified_before` (route) / `--modified-before` (script) is the operator escape
hatch over all of that: it pins the window's end for one run, for digging a
store out of a range by hand rather than waiting for the halving to find it. A
value that is not ISO-8601 is refused rather than ignored — an operator who
mistypes the bound and is answered with an ordinary run would believe they read
a range they did not.

Raising `--max-pages` (capped at 200 on both surfaces) still converges faster
than the bisect; the bisect is what makes an *unattended* run correct.

### The credential must name this store's own site

Before it lists anything, each run calls `GET /1.0/authorization/website` once
and refuses (`SquarespaceSweepError`, logged, **cursor untouched**) if the `id`
it returns is not this store's `website_id`.

Without that check, re-pointing a store from site A to site B while site A's
OAuth token is still in the blob makes the sweep keep listing **site A's**
orders and record them under the store that now represents site B. Nothing
downstream can tell: the orders are well-formed, they simply belong to somebody
else's shop. (Connect drops the old site's OAuth token on a site change, which
closes the same hole from the other end; this is the check that does not depend
on connect having got it right.)

The same call is where the OAuth→API-key fallback happens: a 401/403 on the
OAuth token falls through to the per-site API key, so a store holding both does
not go dark when a short-lived Developer-Platform token expires. Every stored
credential being refused is a loud sweep failure, not a quiet empty run.

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
* **OAuth token → create, then delete.** A fresh subscription is created FIRST,
  and only then is every older subscription pointing at our endpoint removed.
  The replacement is not tidiness: the secret is returned by Squarespace exactly
  ONCE, at creation, and cannot be read back, so reusing a subscription whose
  secret Pivota lost would leave it delivering notifications the receiver can
  only answer 401 to. The **order** is what bounds the blast radius — under
  delete-then-create, a create that fails (a rate limit, an expired OAuth token,
  a Squarespace 5xx) leaves the store with **no subscription at all** until
  somebody notices and re-runs `ensure`. Creating first means the worst case is a
  brief overlap of two subscriptions instead of a gap. A delete that fails after
  a successful create is swallowed and reported as not-replaced: failing the
  whole call there would throw away the one copy of the new secret over a
  leftover subscription whose only symptom is duplicate deliveries the receiver
  401s and the ledger would dedupe anyway.

> **Every `ensure` ROTATES the secret. Do not call it on a schedule.**
> Between the create and the merge that persists the new secret, deliveries
> still signed with the OLD secret answer 401. Squarespace retries them, and
> anything that never lands is picked up by the reconciliation sweep on its next
> run — the sweep is what makes an `ensure` safe to run at all, and the reason
> this is a bounded cost rather than lost telemetry. (Assumption 20: a
> `rotateSecret` endpoint may exist, which would remove the window entirely.)

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
| 4 | `GET /1.0/authorization/website` returns the site the credential belongs to, with `id` and `title`, **and is reachable with a per-site API key** | **Assumed** — the endpoint and its shape are documented, but that it answers an API key (rather than only an OAuth token) is an inference from it not being listed as an OAuth-only surface | This is load-bearing twice over: it is connect's only validation AND the sweep's per-run site check. If an API key gets 401/404 here, **every API-key connect fails and every API-key sweep refuses** — i.e. the whole sweep-only tier is dead, which is the tier that exists for API-key stores. Made diagnosable rather than silent: the connect 400 carries `(upstream HTTP <status>)`, so a 404 says "wrong endpoint" and a 401 says "wrong key" on the first attempt instead of after a support thread. The fix, if it is wrong, is to validate an API-key connect against `GET /1.0/commerce/orders?modifiedAfter=…&modifiedBefore=…` with a one-second window instead, and to bind `website_id` from the OAuth token only |
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
| 17 | `Squarespace-Signature` is HMAC-SHA256 **over the raw request body ALONE**, keyed with the subscription secret | **Verified** (that it is HMAC-SHA256 keyed with the subscription secret) / **ASSUMED** (that the signed input is the body and nothing else — the documented input may concatenate a timestamp header, or the endpoint URL, before the body) | The single most consequential assumption in the receiver. If the input is a concatenation, **every delivery from every Squarespace site 401s, forever**, and the store is silently sweep-only while reporting `webhook_and_sweep`. Nothing is mis-recorded — the sweep still reconciles — but push telemetry is dead and, without the diagnostic below, indistinguishable from a wrong secret. So a rejected delivery logs at WARNING the **names** of the `Squarespace-*` headers present (never values) and whether the digest arrived hex- or base64-shaped: a `Squarespace-Timestamp` in that list, on a store whose secret is known good, is the tell. The fix is then a one-function change in `_valid_signature` |
| 18 | That signature is **hex**-encoded | **Assumed** | Hedged as widely as the encoding could plausibly go: hex (either case), standard base64, url-safe base64, and both base64 forms unpadded, all compared under `hmac.compare_digest` with no early return. Widening costs nothing — a caller must still produce the digest, which needs the secret — while narrowing, if the guess is wrong, 401s every delivery a site ever sends |
| 19 | Notification body `{id, topic, createdOn, websiteId, subscriptionId, data: {orderId}}`, thin | **Verified** | A body with no `data.orderId` is 422 and nothing is recorded |
| 20 | There is no rotate-secret endpoint | **Assumed, and specifically doubted**: `POST /1.0/webhook_subscriptions/{id}/actions/rotateSecret` is believed to exist | Not used, because its existence and response shape are unverified and a wrong guess here fails an `ensure` that is otherwise working. The cost of not using it is that every `ensure` **rotates the secret by replacing the subscription** (see "Provisioning the subscription"). If it does exist, adopting it removes the rotation window entirely and is a contained change in `services/squarespace_webhook_subscriptions.py` |
| 21 | A Developer-Platform **OAuth access token is short-lived** (~30 minutes) and refreshes via a rotating refresh token | **Assumed (medium confidence)** | There is **no refresh path in this repo**. If the lifetime is short, an OAuth store's token goes stale and every read with it 401s. Handled, not fixed: reads try the OAuth token first and fall back to the per-site API key on 401/403 (order fetch, orders list, and the site lookup), so a store holding both keeps working; a store holding **only** an OAuth token goes dark until someone reconnects. `oauth_refresh_token` and `oauth_expires_at` are persisted when the connect request supplies them, so implementing the refresh does not require every OAuth store to reconnect first. See Residual gaps |

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
* **No OAuth refresh path.** A Squarespace Developer Platform access token is
  short-lived (assumed ~30 minutes, with a rotating refresh token — assumption
  21), and **nothing in this repo refreshes one**. What exists instead is a
  fallback: every READ (order fetch, orders list, the per-run site lookup) tries
  the OAuth token first and falls back to the per-site API key on 401/403,
  logging the fallback once per run with no values. So a store that holds
  **both** credentials keeps working indefinitely; a store that holds **only** an
  OAuth token goes dark — reads 401, the sweep fails loudly, deliveries 503 —
  until a human reconnects it. `oauth_refresh_token` and `oauth_expires_at` are
  persisted when the connect request supplies them, purely so that implementing
  the refresh later does not require every OAuth store to reconnect first.
  Closing this means a token-refresh call before expiry plus a merge of the
  rotated pair into the credential blob, inside the same critical section the
  rest of the blob's writes already use.
* **`ensure` rotates the secret every time.** See the note above; in-flight
  deliveries signed with the previous secret 401 until Squarespace's retries or
  the sweep recover them.
* **The per-store reconcile guard is per-PROCESS.** `POST /reconcile` refuses a
  409 while a sweep for that store is already running in the same worker, which
  stops the operator double-click and the retry-on-timeout. It does not
  coordinate across replicas; what makes a genuinely concurrent sweep safe is
  the row lock in `merge_squarespace_credentials`, not that guard.
* **`order.paid` is inferred, not observed.** See assumption 11.

# Webflow commerce telemetry

Two facts about the Webflow Data API v2 shape this whole integration, and
neither has a precedent in this repo.

1. **Webflow signs only SOME webhook deliveries.** A webhook created by an OAuth
   App (a Data Client) is signed with that App's client secret; a webhook created
   with a Site API token — which is how a merchant connects one site without
   Pivota shipping a Webflow app — is delivered **unsigned**. A signature check
   alone would therefore reject every site-token store, or, made optional,
   authenticate nothing. So the receiver has **two layers**, and the one that is
   always on is a per-store secret in the URL.
2. **The orders list has no modified-since filter and no documented ordering.**
   `GET /v2/sites/{id}/orders` takes `status`, `offset` and `limit` and nothing
   else. A time-windowed sweep of the Squarespace/Cafe24 kind is impossible; what
   exists instead is a resumable offset walk in three lanes, whose early stop is
   **armed by an observation** rather than by an assumption.

A third fact makes this integration *simpler* than its siblings: **Webflow
refunds are full-order only.** There is one refund per order, for the whole
`customerPaid` amount, so there is no cumulative total, no delta arithmetic, no
baseline read, and therefore **no money lock anywhere in this bridge**. That is
a property of the platform, not an omission; `tests/test_webflow_ledger.py`
pins it so a future partial-refund feature has to change the assertion
deliberately.

| Piece | Where |
| --- | --- |
| Connection | `POST /integrations/webflow/connect` (`routes/merchant_store_connections.py`) |
| Receiver | `POST /webhooks/webflow/{store_id}/{url_secret}` (`routes/webflow_webhooks.py`) |
| Order read-back / list | `services/webflow_order_fetch.py` |
| Mapper (pure) | `services/webflow_event_adapter.py` |
| The one ingest path | `services/webflow_ledger.py` |
| Sweep | `services/webflow_order_sweep.py` |
| Sweep, as a route | `POST /integrations/webflow/{store_id}/reconcile` |
| Sweep, as a script | `scripts/sweep_webflow_orders.py` |
| Webhook provisioning | `POST /integrations/webflow/{store_id}/webhooks/ensure`, `services/webflow_webhook_subscriptions.py` |
| Credential shape / site binding | `services/webflow_connection.py` |
| The atomic credential merge (shared) | `services/merchant_store_credentials.py` |

No schema change. The credential blob lives in `merchant_stores.api_key` as
JSON, exactly like BigCommerce, PrestaShop and Squarespace:

```json
{"api_token": "...", "site_id": "...", "site_name": "...",
 "url_secret": "...", "webhook_ids": {"ecomm_new_order": "..."},
 "reconciliation": {"orders":   {"cursor": "...", "next_offset": 0, "ordering_verified": true},
                    "refunded": {"cursor": "...", "next_offset": 0, "ordering_verified": true},
                    "dispute_lost": {"...": "..."},
                    "last_run_at": "...", "overlap_minutes": 60}}
```

## Money is already in minor units

```json
"customerPaid": {"unit": "USD", "value": 5898, "string": "$58.98"}
```

The currency lives under **`unit`**, not `currency`, and **`value` is an integer
number of minor units**. `services/webflow_event_adapter.py` therefore performs
**no conversion at all**, and that absence is the most consequential thing about
it: a `* 100` "for consistency with every other adapter in this repo" would file
a $58.98 order as $5,898.00, and nothing downstream distinguishes an inflated
amount from a real one.

Because there is no conversion, there is also **no zero-decimal-currency table**
— and that is an **assumption**, not a consequence. See row 22: it is only
correct if Webflow reports a ¥5,898 order as `value: 5898` rather than as
`589800`, and this repo has never seen a Webflow order in a zero-decimal
currency. If Webflow reports hundredths there, such an order is filed **100x
inflated** and nothing downstream can tell it from a real one, because
`customerPaid` is the only source for the figure.

So the mapper carries a **tripwire** rather than an argument: the first order
per process per zero-decimal currency (`utils.money.ZERO_DECIMAL_CURRENCIES`)
logs a WARNING naming the store, the currency, the order and the observed
`value`, so the first such order is visible instead of silently doubling as
evidence. It is keyed on the currency, whose domain is that frozen 16-element
set, so the bookkeeping is bounded by construction.

A `value` the mapper cannot read as a whole number of minor units — a decimal
string like `"58.98"`, a fractional float, a negative — is a **loud**
`WebflowMoneyFormatError`, not a skipped event. A silent skip under-counts; a
misread over-counts by 100x, and only one of those is visible in a total. The
receiver turns it into a 422 and the sweep counts it as `invalid`.

## Receiver auth: two layers

```
POST /webhooks/webflow/{store_id}/{url_secret}
x-webflow-timestamp: 1757160000000            # OAuth-app deliveries only
x-webflow-signature: <hex HMAC-SHA256 of "{timestamp}:{raw body}">

{"triggerType": "ecomm_new_order", "payload": { …the whole order… }}
```

**Layer 1 — the URL secret, always.** A 256-bit `secrets.token_urlsafe(32)` is
minted at provisioning and embedded as a path segment. It is compared
constant-time **as bytes** against the store's blob. Missing store, inactive
store, unprovisioned store and wrong secret all answer the same 401, so a caller
learns nothing about which it hit. Comparing as bytes is not fussiness: Starlette
decodes path segments as text, and `hmac.compare_digest` raises `TypeError` on a
str with non-ASCII code points — a hostile URL would otherwise be a **500**,
which is a denial-of-service handle rather than a refusal.

**Layer 2 — the signature, when `WEBFLOW_CLIENT_SECRET` is configured.** Then the
`x-webflow-signature` header is *required* and verified as HMAC-SHA256 over
`"{timestamp}:{raw body}"` with the App's client secret, inside a 5-minute skew
window. The timestamp is part of the signed input **and** is checked for
freshness, so a captured delivery cannot be replayed after the window closes.
When the env var is absent the layer is skipped entirely — a deployment with no
Webflow app has no client secret to check against, and requiring one would 401
every site-token store.

> A deployment that runs an OAuth app should set `WEBFLOW_CLIENT_SECRET`. Without
> it, the URL secret is the only thing authenticating a delivery. That is
> genuinely sufficient — it is a 256-bit secret over TLS — but it has no replay
> window and no rotation short of re-provisioning, **and it is in a URL**.

### A secret in a path, and where that path is written down

A secret carried in the URL path is written into request logs the way a secret
carried in a header is not. So:

* **In this application, it is redacted.** `StructuredLoggingMiddleware` logs
  `path` on **every** request — INFO on the 200s, WARNING on the 401s — and it
  logged this one verbatim until `middleware/structured_logging.py::redact_path`
  rewrote it to `/webhooks/webflow/{store_id}/[REDACTED]`. The redaction is a
  small registry of `(prefix, segments_to_keep)` pairs rather than a Webflow
  special case, so the next path-secret route registers a prefix instead of
  growing a second rule. The rate limiter's anonymous-ceiling warning — the one
  other logger an unauthenticated request to this path can reach — goes through
  the same helper. The receiver's own 401 line and `telemetry_ingress`'s
  non-2xx line never carried the path at all; they name the store id and the
  logical write path. `tests/test_webflow_webhooks.py` drives the receiver
  through the real middleware over `ASGITransport` and asserts the secret is
  absent from every captured record, on the 200 path and the 401 path.
* **Upstream of this application, it is not, and cannot be.** The load
  balancer's access log, any proxy in front of it, and an APM trace all hold the
  full path. Nothing in this repo can redact those. That is the honest argument
  for setting `WEBFLOW_CLIENT_SECRET` wherever an OAuth app exists: Layer 2
  requires a fresh signature over the body, which a URL recovered from an access
  log does not provide.

Every malformed input on either layer is a 401, never a 500 and never a 200: an
empty header, a non-numeric timestamp, non-ASCII bytes, a signature over the body
alone. `tests/test_webflow_webhooks.py` pins each.

**A rejected delivery logs what makes a wrong assumption diagnosable.** At
WARNING: which layer refused it, whether the store was known, whether a secret
was provisioned, whether the digest arrived hex-shaped, and the **names** of the
`x-webflow-*` headers present — never a value. Assumption 12 (what exactly is
signed) is unverified; if it is wrong the symptom is a 401 on every signed
delivery, which without this line is indistinguishable from a wrong secret.

**Rotation.** `ensure?rotate=true` mints a new secret and re-registers the
webhook at the new URL. In-flight deliveries to the OLD URL answer 401; Webflow
retries them, and anything that never lands is recovered by the reconciliation
sweep. Without `rotate`, an existing secret is REUSED, which is what makes
`ensure` safe to re-run after a partial failure.

## The delivery is a trigger, not a fact

Webflow puts the whole order in the body. This bridge reads exactly two things
out of it — the trigger type and the order id — and then **fetches** the order
from `GET /v2/sites/{site_id}/orders/{order_id}` and maps that.

Layer 1 proves the sender knows a secret; it does not make the sender's
arithmetic Webflow's. And the fetch URL carries **this store's own `site_id`**,
so an order id belonging to another site cannot be read through this store's
credential even if a delivery names one — that is the structural half of the site
binding. The payload's `siteId`, when it carries one, is bound to the store's as
well (bytes compare, 401 on mismatch); that is the diagnostic half, and it exists
because Webflow is not documented to put a `siteId` on an ecomm payload.

The order id is validated against `^[A-Za-z0-9_-]{1,64}$` **and** percent-encoded
before it reaches a URL path. Everything that arrives here is
attacker-influenced, and a signature (when there is one at all) proves the sender,
not the shape of a field: an id of `../../token/introspect` would walk the path
out of the orders collection and make the fetch read a different endpoint.

A failed fetch is **503**, never 200 — Webflow retries a non-2xx, and a 200 would
drop the event until the sweep's next run. A 404 is 503 too: it is usually the
read racing the delivery, and a permanently absent order exhausts Webflow's
retries and is then recovered by the sweep.

**The delivery dedupe is keyed on the BODY, not the order.** A Webflow order
legitimately changes state several times (`pending` → `unfulfilled` → `refunded`)
and each is a different `ecomm_order_changed` for the same order; keying the
per-process LRU on the order id would swallow the refund. A true redelivery
repeats the body byte-for-byte. A short-circuited redelivery answers
`duplicates: 1`, not 0 — it IS a duplicate observation, and reporting zero would
make the metric read as if redeliveries never happen. The actual correctness
guarantee is the ledger's deterministic event ids, which hold across processes,
restarts, and the sweep.

## Mapping

| Canonical event | When | `occurred_at` | `amount_cents` |
| --- | --- | --- | --- |
| `order.created` | always | `acceptedOn` (else now) | `customerPaid` |
| `order.paid` | `status` ∈ {unfulfilled, fulfilled, disputed, dispute-lost, refunded} and a positive amount and currency | `acceptedOn` | `customerPaid` (`native_amount_semantics=customer_paid`) |
| `refund.succeeded` | `status` ∈ {refunded, dispute-lost} **and** a positive amount and currency | `refundedOn` / the dispute timestamps | the full `customerPaid` |
| `order.cancelled` | **never** | — | — |

**An unreadable refund costs the refund row, not the order.** A
`refunded`/`dispute-lost` order whose `customerPaid` is absent or `0` still
records `order.created`; the refund becomes the named reason
`refund_amount_unreadable`. Raising there dropped `order.created` too and 422'd
the receiver — and Webflow retries a 422 into the same 422, so the order never
landed at all. The reason is counted (`refunds_unreadable` per lane and per run),
logged at WARNING, and printed as its own NOTE by the script, because refunded
GMV really is under-reported until the amount becomes readable. A *malformed*
value (`"58.98"`, a fractional float, a negative) is still fatal to the whole
observation — that is the 100x claim and it must never be half-recorded.

**`pending` is not paid.** It is Webflow's documented unpaid state (a PayPal
payment awaiting capture), so it gets `order.created` alone. A later
`ecomm_order_changed` — or the sweep — completes it: because the event ids are
deterministic, that later observation adds `order.paid` beside the SAME
`order.created` row rather than duplicating the order.
`tests/test_webflow_ledger_end_to_end.py` walks the whole `pending → unfulfilled
→ refunded` lifecycle and asserts each event lands exactly once.

**Webflow has no cancelled state, and none is invented.** Deriving
`order.cancelled` from `refunded` would file a refund as a cancellation and count
one order in two funnels.

**`disputed` emits no money event.** The funds are HELD pending the outcome, not
returned. Recording a refund there and another if the dispute is later lost would
count the same money out twice; recording one and never correcting it if the
dispute is WON would invent a refund that never happened. The status is kept as
`native_status` metadata on the order's own events.

**`dispute-lost` IS money out, and it shares the refund's key.** It is emitted as
`refund.succeeded` for the full `customerPaid`, under the same entity key as an
ordinary refund: `<orderId>:refund`. The two statuses are mutually exclusive on
one order at any instant, but an order can MOVE between them across observations,
and two keys would then record the same money twice — the funnel sums refund
rows. One key makes "at most one refund row per order" structurally true, and the
amounts are identical either way. `native_amount_semantics` distinguishes them
(`full_order_refund` vs `dispute_lost_full_amount`).

**The PSP's refund id is metadata, never the key.** `stripeDetails.refundId` is
present for a Stripe order and absent for a PayPal one; an order first observed
without it and later with it would land on two different keys — two rows for one
refund. It is carried as `native_psp_refund_id` (and `native_psp_dispute_id`) so a
ledger refund can be reconciled against the PSP by hand.

`order_ref` is `webflow:<orderId>`. **Event ids are derived from the order id and
the event type**, never from the delivery, which is what makes a webhook
observation and a later sweep observation of the same order collapse onto one
ledger row instead of counting every purchase twice.

**No buyer identity, ever.** `customerInfo` is a name and an email;
`stripeDetails.customerId` is a PSP identity for a natural person. None reaches
the ledger. `customData` is buyer-entered free text and is **not** read as a
Pivota order marker: a forged `pivota:` string must not be able to merge an order
into an interaction it does not own (the BigCommerce reasoning). Line items keep
join keys (`productId`, `variantId`, `variantSKU`) and `count` — and deliberately
**no amount**: the ledger's line-item vocabulary spells amounts as
`price`/`subtotal`/`total` and every other adapter writes a decimal string there,
so a minor-unit integer under those names would plant exactly the 100x ambiguity
this adapter refuses everywhere else, for a field that is only a diagnostic.

## The sweep

```
POST /integrations/webflow/{store_id}/reconcile?apply=true&max_pages=10
POST /integrations/webflow/{store_id}/reconcile?lane=refunded
python -m scripts.sweep_webflow_orders --store-id store_x --apply
python -m scripts.sweep_webflow_orders --lane refunded --apply
```

### Three lanes, because there is no modified-since filter

| Lane | `status` filter | anchored on | ordering claim applies? |
| --- | --- | --- | --- |
| `orders` | *(none)* | `acceptedOn` | **yes** — the list's own sequence |
| `refunded` | `refunded` | `refundedOn` | no — never armed, never judged |
| `dispute_lost` | `dispute-lost` | `disputeUpdatedOn` / `disputedOn` | no — never armed, never judged |

The anchor must be the field that MOVES when the thing the lane looks for
happens. `acceptedOn` never changes after an order is accepted, so it can anchor
the new-order lane but could never find a refund of a year-old order — that
refund would sit below any cursor the orders lane had already passed. Hence the
separate, short, cheap money-out lanes.

**A lane whose `status` filter Webflow rejects fails ALONE**, is reported in
`lane_failures`, and the run reports `partial_failure`. `dispute-lost` as a query
value is an assumed claim (row 8); if it is wrong that must not take down the lane
that reads new orders.

### The ordering claim is earned, not assumed

The early stop — end the pass at the first page whose every order is at or below
`cursor − overlap` — is **armed only by a previous COMPLETE pass that saw the
anchors arrive non-increasing**, and is disarmed the moment a run observes a
violation.

Checking only within the current run is not enough, and the gap is not
theoretical: a run whose first page happened to be entirely below the threshold
would stop there having seen a perfectly ordered two-row prefix, and never reach
the out-of-order row further down. So a lane with no verdict — a store's first
ever pass, or one after a violation — **walks the whole list**, and that walk is
what establishes the verdict. A truncated pass cannot establish one: a violation
is proof, but "no violation seen" is only proof when the whole list was read.

**And it applies to the `orders` lane alone.** Assumption 7 is about
`acceptedOn`, which is what an offset walk of the unfiltered list arrives in. The
money-out lanes anchor on `refundedOn` / the dispute timestamps, which bear no
relation to the list's sequence, so their anchors arrive in essentially arbitrary
order. Judging them made almost every store report a "violation" on almost every
run, and the script's NOTE fired every time — which is the same as it never
firing, and it would have buried a genuine `orders`-lane violation. So those
lanes are **never armed and never judged**: they walk their short, filtered list
in full (bounded by the page cap, resuming on truncation like any other lane) and
report `ordering_verified: null` with `ordering_applicable: false`, rather than a
verdict they cannot earn. A stale `ordering_verified` left in their stored state
by an earlier build is *removed*, not merely ignored, so it can never re-arm a
stop they must not take.

`ordering_applicable`, `ordering_verified` and `early_stop_armed` are reported
per lane per run, so the assumption is falsifiable from a real run rather than
from the documentation. The run-level `ordering_verified` aggregates the judged
lanes only (`null` when none ran), `unordered_lanes` names the offenders, and the
script's NOTE prints store/lane pairs.

### Truncation resumes; it never freezes

A lane stopped by the page cap does **not** advance its cursor — the pass was
incomplete, so orders below the maximum anchor it saw may still be unread.
Instead it records the offset it reached (`next_offset`), and the next run
**resumes there** rather than restarting at 0 and re-reading the same page-cap
prefix forever. (That freeze is the trap the Squarespace review found in the
hold-the-cursor design; here the fix is a resume rather than a bisect, because
the API is offset-paged rather than time-windowed.)

Resuming from an offset is safe in the direction that matters under either
plausible ordering: **newest-first**, new orders shift the list DOWN, so a resume
re-reads rows it has already seen (deterministic ids dedupe them) and skips none;
**oldest-first**, new orders append and the resume is exact. Under a genuinely
unstable ordering it could skip — see Residual gaps.

### Cursor safety, per lane

* a **completed** pass advances the cursor to the highest anchor it saw, and only
  if that is later than the stored one — a back-dated order or a clock skew can
  never re-open a closed pass;
* an **incomplete** pass advances nothing;
* the early-stop threshold is `cursor − overlap` (default 60 minutes), so an
  order written in the same second the last pass ended is re-read rather than
  skipped. Re-reading is free: the event ids are deterministic and the ledger
  dedupes;
* an order whose anchor is at or below `cursor − overlap` is **skipped** rather
  than re-ingested. That is a cost saving, not a correctness claim: the cursor
  only advances on a completed pass, so such an order has already been seen, and
  re-recording it would dedupe anyway.

### The credential must name this store's own site

Before it lists anything, each run calls `GET /v2/sites/{site_id}` once and
refuses (`WebflowSweepError`, logged, **every cursor untouched**) if the id it
returns is not this store's. The list URL already carries the site id, so a token
for another site 403s rather than returning someone else's orders — but a store
whose binding was lost or hand-edited would otherwise build a request out of an
empty path segment, and a revoked token should fail the run loudly rather than as
an empty sweep that reports success.

`--apply` is required to write anything; the default is a dry run that lists and
classifies and moves no cursor. A run that fails mid-lane persists no state for
that lane.

## Scheduling — the gap

**Nothing schedules this sweep automatically today, and that is deliberate rather
than an oversight.** Two facts from this repo:

* CI deploys **no Cloud Run job** for this lane. `deploy-prod` does not build or
  ship one, so adding a job entry here would be a scheduled run that never
  exists.
* The APScheduler lane (`services/audit_scheduler.py`, where
  `cafe24_reconciliation` lives) runs inside the `worker` service, which is not
  auto-deployed on merge. Registering a Webflow tick there would inherit the same
  unproven lane.

So the sweep ships with **two reachable, authenticated surfaces and no
scheduler**: the `reconcile` route (merchant-or-staff, ownership checked from the
row) and the script. Until a scheduled run exists, the sweep's latency is the
interval at which one of those is invoked — which matters more here than for a
platform with guaranteed delivery, because Webflow webhooks are best-effort.
Wiring it up is a follow-up that should register the job in
`services/audit_scheduler.py` alongside `cafe24_reconciliation`, add its id to
`_RUNNABLE_JOB_IDS` in `routes/admin_scheduler_jobs.py`, and — separately — make
the lane actually deploy.

## Connect

```
POST /integrations/webflow/connect
{"merchant_id": "...", "api_token": "...", "site_id": "...?",
 "store_name": "...?", "domain": "...?"}
```

The token is validated by resolving the **site** it reaches, which is also the
binding step. With an explicit `site_id` that site is looked up and the token is
proven to reach it. Without one, the site is resolved **only if the token reaches
exactly one**; zero or several is a **409 `site_selection_required`** listing the
candidate ids and names. Guessing would bind the wrong shop, and every order
swept afterwards would be filed under it — well-formed rows that nothing
downstream could flag.

**The SITE is the store's identity, not the domain.** The existing-store lookup
consults this merchant's Webflow rows for one already bound to the resolved
`site_id`, and falls back to `(merchant_id, platform, domain)` only when no
site-bound row exists (a row written before the binding did). Keying on `domain`
alone — which is caller-supplied, and otherwise merely derived from `shortName` —
meant a second connect for the SAME site with a different explicit `domain`
created a SECOND store bound to that site; both would then sweep the same order
list and the funnel would count the site's GMV **twice**, out of rows that are
individually well-formed and impossible to flag downstream. A reconnect matched
on the site answers with the ROW's domain rather than the request's.
*(Squarespace's connect has the same shape on `website_id` — a follow-up, not
touched here.)*

A connect that fails names the **upstream status** in its 400 detail
(`… (upstream HTTP 404)`), so a wrong assumption about the endpoint is separable
from a mistyped token on the first attempt.

**A reconnect read-modify-writes the blob, inside ONE critical section**, via the
shared `merge_store_credentials`. The same cell holds the token, the site binding,
the URL secret (the value baked into the webhook URL registered at Webflow) and
every lane's cursor; overwriting it is verbatim the PrestaShop P1.

**A reconnect to a DIFFERENT site drops `WEBFLOW_SITE_SCOPED_KEYS` in full** —
`api_token`, `site_id`, `site_name`, `url_secret`, `webhook_ids`,
`reconciliation`. The credential is the dangerous member and the easiest to miss:
it is what every read uses, so a surviving old-site token keeps the sweep reading
the OLD site and recording its orders under the store that now represents the new
one. That is the Squarespace review's finding, and it is closed structurally
rather than by a hand-listed subset:
`tests/test_webflow_connection.py::test_every_credential_the_read_path_prefers_is_dropped_on_a_site_change`
compares `WEBFLOW_TOKEN_KEYS` (what `webflow_read_tokens` reads) against
`WEBFLOW_SITE_SCOPED_KEYS` (what the drop removes), so a second credential added
later fails there rather than quietly escaping.

`telemetry_mode` in the response is read off the blob **that persisted**, so a
reconnect to the same site still reports `webhook_and_sweep` if its provisioning
survived. The token is never logged.

### The credential blob is written under a row lock

`services/merchant_store_credentials.py::merge_store_credentials` is the ONE
writer of `merchant_stores.api_key` for this platform (and for Squarespace, which
was generalized into it). It runs read → mutate → write → re-read inside
`database.transaction()` behind `SELECT … FOR UPDATE` on Postgres; a plain select
on SQLite, which has no `FOR UPDATE` and where this is tests and local
development only.

Without the lock, two writers both read the pre-write blob and the second
silently discards the first: a sweep persisting its cursors between `ensure`'s
read and its write **erases the `url_secret`** — and losing that does not rotate a
secret, it leaves Webflow delivering to a path the receiver can only 401 until
someone re-provisions, because the secret lives in a URL Pivota registered rather
than in anything Webflow will tell us. The reverse interleaving reverts a
reconnect to the token the merchant just replaced. Both are pinned in
`tests/test_webflow_ledger_postgres.py` with two genuinely separate backends,
because SQLite cannot observe either.

The re-read is not belt-and-braces: `databases` + asyncpg reports no rowcount
from an `UPDATE`, so reading the row back is the only proof the write landed —
and under a race, the only way `ensure` learns whether ITS write won.

## Provisioning the webhooks

```
POST /integrations/webflow/{store_id}/webhooks/ensure[?rotate=true]
```

Merchant-or-staff role gate; ownership comes off the fetched ROW, because
`store_id` is caller-supplied and the SELECT keys on it alone. One in-flight
`ensure` per store (409 `ensure_already_running`) — two racing would register two
different URLs, of which only the last-persisted secret authenticates.

**The order of the two writes is the whole design:**

```
persist the URL secret  ->  register the webhook at Webflow  ->  persist the ids
```

A crash between the first and second step leaves a stored secret and no webhook:
harmless **on first provisioning**, and fixed by re-running. The opposite order
would leave Webflow delivering to a URL whose secret Pivota never stored, and the
receiver would 401 every one of them forever. After the second merge the
persisted secret is compared against the one that was registered; if another
writer replaced it, the just-registered webhooks are deleted, an
`action=lost_race` line names the store, the owning merchant, the actor and the
discarded webhook ids, and the call answers 409 rather than leaving them
delivering into a wall.

**A ROTATION IS THE EXCEPTION, and "harmless" does not carry over to it.**
`rotate=true` runs against a store whose webhook already WORKS, so a failure
after the persist leaves the live webhook on the OLD secret while the new one is
stored — and every delivery 401s until someone re-runs `ensure`. A registration
that fails (502, 504, `scope_required`) therefore **restores the superseded
secret** and logs `action=rotation_rolled_back`; the restore is guarded on the
stored value still being the one this run minted, so it can never clobber a
concurrent writer. A process killed between the persist and that handler is a
residual gap, listed below.

**Create first, then delete.** A webhook is created for every wanted trigger
(`ecomm_new_order`, `ecomm_order_changed`) before any stale one is removed, so a
failed create cannot leave the store with no webhook at all; the worst case is a
brief overlap whose only symptom is a duplicate delivery the dedupe absorbs. A
delete that fails afterwards is swallowed and counted in `stale_removal_failures`.

**"Stale" is deliberately narrow**: a webhook whose URL **starts with** one of
OUR OWN prefixes for this store — same origin, same `/webhooks/webflow/{store_id}/`
prefix, different secret — or a duplicate of a trigger we now hold once. A webhook
pointing anywhere else belongs to another integration of the merchant's and is
left alone; deleting their Zapier hook because it was in the list would be a
destructive answer to a provisioning request. The match is **anchored**, not a
substring test: `prefix in url` also fires on any URL that merely *contains*
ours — a redirector or proxy of the merchant's whose target is this endpoint, or
a staging deployment that embeds the prod URL — and the answer to a match here is
DELETE.

**A token without the webhook scope is 409 `scope_required`**, naming
`webhooks:read` / `webhooks:write` and pointing at the reconcile path. A 502
would tell the merchant to retry; what they actually need is to re-issue the
token.

**The secret is never returned, and no application logger writes it.** The
response says `secret_provisioned: true`, which triggers were created or reused,
and how many stale webhooks were removed. Every outcome — `provisioned`,
`scope_required`, `lost_race`, `rotation_rolled_back` — is logged at INFO with
the store, the owning merchant, and the **actor's role and user id**, because
minting and rotating a credential is a staff-capable action. The receiver's own
access log redacts the secret out of the path; upstream infrastructure logs still
hold it (see "A secret in a path", above).

The callback origin comes from `WEBFLOW_WEBHOOK_BASE_URL`, `PUBLIC_BASE_URL` or
`PIVOTA_BACKEND_BASE_URL` and must be HTTPS with no credentials, query or
fragment, or the call is 503.

## Verified vs assumed

"Verified" means checked against the Webflow Data API v2 / Ecommerce
documentation and the platform's behaviour as of 2026-09-06. "Assumed" means the
code depends on it and it is **not** confirmed — each row says what happens if the
assumption is wrong.

| # | Claim | Status | If wrong |
| --- | --- | --- | --- |
| 1 | Base URL `https://api.webflow.com/v2/`, auth `Authorization: Bearer <token>` | **Verified** | Every call fails at connect, immediately and loudly |
| 2 | A Site API token (Site settings → Apps & integrations → API access) and an OAuth App token both reach `/v2/sites/...` with the right scopes (`ecommerce:read`, `sites:read`, `webhooks:read`/`write`) | **Verified** | A scope failure is a 403, surfaced as 409 `scope_required` for webhook calls and as a loud sweep failure for reads |
| 3 | Rate limit ≈60 requests/min per token; 429 with `Retry-After` | **Verified** | Surfaces as a retryable fetch/sweep error either way; the `Retry-After` is named in the error text |
| 4 | `GET /v2/sites` lists reachable sites with `id`, `displayName`, `shortName`; `GET /v2/sites/{id}` returns one | **Verified** | Connect fails with the upstream status in its detail; nothing is bound |
| 5 | `GET /v2/sites/{id}/orders` accepts `status`, `offset`, `limit` (≤100) and returns `{"orders": [...], "pagination": {...}}` | **Verified** (params) / **Assumed** (the collection key) | `items` is accepted as a second spelling. If it is neither, every lane reads an empty page and completes immediately — a silent no-op, whose tell is a store with `seen: 0` and a real order history |
| 6 | There is **no** modified-since / updated-since filter and no cursor on the orders list | **Verified** (by absence) | If one exists, the whole lane machinery could be replaced by a window and the sweep would get much cheaper. Nothing is mis-recorded meanwhile |
| 7 | The orders list is returned **newest-first** | **ASSUMED, and specifically not trusted.** The early stop is armed only by a COMPLETE pass that observed non-increasing anchors, and disarmed by any observed violation | If the ordering is unstable rather than merely different, a resumed offset walk could skip rows. The mitigations are the overlap, the fact that a violation permanently disarms the early stop, and `ordering_verified` being reported per run. See Residual gaps |
| 8 | `status` ∈ `pending`, `unfulfilled`, `fulfilled`, `disputed`, `dispute-lost`, `refunded`, and `status=dispute-lost` is a valid query value | **Verified** (the enum) / **Assumed** (that it filters) | The `dispute_lost` lane fails ALONE and is reported in `lane_failures`; the other two lanes are unaffected, and a lost dispute is still mapped whenever the webhook or the unfiltered lane sees the order |
| 9 | Order fields `orderId`, `status`, `acceptedOn`, `fulfilledOn`, `refundedOn`, `disputedOn`, `customerPaid`, `netAmount`, `customerInfo`, `purchasedItems[]`, `purchasedItemsCount`, `stripeDetails`, `paypalDetails`, `paymentProcessor`, `metadata`, `customData` | **Verified** | A missing money field yields no money event rather than a zero one; a missing `orderId` is a 422. A `refunded`/`dispute-lost` order whose `customerPaid` is absent or `0` does **not** fail the observation: `order.created` is still recorded and the missing refund is reported as the named reason `refund_amount_unreadable` (`WebflowMapping.ignored`, `WebflowIngestResult.ignored_reasons`, the sweep's `refunds_unreadable` counter, a WARNING from `services/webflow_ledger.py`, and a NOTE from the script). Raising instead dropped the whole batch, which 422'd the receiver — and Webflow retries a 422 into the same 422 until it gives up, so the order never landed at all. Refunded GMV is under-reported meanwhile, which is why it is counted and named rather than swallowed |
| 10 | Money is `{"unit": "USD", "value": <integer minor units>, "string": "$58.98"}` | **Verified** | This is the 100x claim. It is pinned with the documented example in `tests/test_webflow_event_adapter.py`, and a `value` that is not whole minor units is REFUSED rather than guessed at, so a shape change is loud rather than silently inflationary |
| 11 | `orderId` is a short opaque token (`0000-0001`-shaped hyphenated groups) | **Assumed** | The path allowlist is `^[A-Za-z0-9_-]{1,64}$`, which is wider than that shape and still cannot walk a URL path. An id outside it is refused rather than encoded-and-sent, so the failure mode is a refused fetch, not a request to the wrong endpoint |
| 12 | Webflow signs a delivery with `x-webflow-timestamp` + `x-webflow-signature` = hex HMAC-SHA256 over `"{timestamp}:{body}"`, keyed with the OAuth **App's client secret**, and **only** for webhooks created by an OAuth App | **Verified** (the algorithm and the input) / **Assumed** (that a Site-API-token webhook is unsigned) | If site-token webhooks ARE signed, Layer 2 could be required unconditionally and Layer 1 would be belt-and-braces — no correctness loss either way. If the signed INPUT is not `"{timestamp}:{body}"`, every signed delivery 401s on a deployment that armed Layer 2; the rejection log names which layer refused it and the shape of the digest, which is what makes that decidable from one line rather than a packet capture. Layer 1 keeps working meanwhile |
| 13 | The replay window is 5 minutes and `x-webflow-timestamp` is epoch **milliseconds** | **Assumed** | Seconds are accepted too (a value below 10^10 is read as seconds), because guessing wrong in that direction would reject every delivery. The unit affects only the freshness check |
| 14 | The v2 delivery envelope is `{"triggerType": ..., "payload": <order>}` | **Assumed** | A bare order body is accepted as well. If it were neither, every delivery would be 422 "missing an order id" — loud, and recoverable by the sweep |
| 15 | Trigger types `ecomm_new_order` and `ecomm_order_changed` exist and cover order creation and every state change | **Verified** | An unmapped trigger is ignored BEFORE the fetch and costs no API call. A missing state-change trigger would mean refunds arrive only via the sweep |
| 16 | `POST /v2/sites/{id}/webhooks` `{triggerType, url}`, `GET /v2/sites/{id}/webhooks`, `DELETE /v2/webhooks/{id}` | **Verified** | `ensure` fails with the platform's status; the URL secret is already persisted, so a re-run is safe |
| 17 | A **Site API token** can create webhooks (given `webhooks:write`) | **Assumed (high confidence)** | If it cannot, `ensure` answers 409 `scope_required` naming the scope, and the store is sweep-only. Nothing is mis-recorded; the doc's promise of push telemetry for site-token stores would be wrong |
| 18 | **Refunds are FULL-ORDER only** (`POST /v2/sites/{id}/orders/{id}/refund`); there are no partial refunds and no per-refund records | **Verified** | This is the load-bearing simplification. If partial refunds exist, every refund after the first is recorded as a full-amount duplicate under one key — i.e. **under**-counted, not over-counted. The fix is the Shoplazza/Squarespace cumulative-delta machinery: a baseline read via `recorded_refund_amount_cents` across both write paths, a `<order>:<cumulative>` key, and `order_money_read_modify_write_lock` around the pair. `tests/test_webflow_ledger.py` asserts none of that is present today, so adopting it is a deliberate change rather than a drift |
| 19 | A `refunded` order carries `refundedOn`; a disputed one carries `disputedOn` (and possibly `disputeUpdatedOn`) | **Assumed** | The mapper falls through to `acceptedOn`, so a refund is anchored early rather than lost. The refund lane's cursor would then track `acceptedOn` and the lane would keep re-reading the same window — visible as a lane that never advances |
| 20 | An **accepted** Webflow order (any status but `pending`) has been paid for | **Verified** (that `pending` is the unpaid state) / **Assumed** (that nothing else is unpaid) | If another status can be unpaid, `order.paid` overstates GMV for those orders. `pending` covers the documented case |
| 21 | Webflow does **not** flag test orders (there is no test mode; a sandbox is a separate site with its own site id) | **Assumed** | Guarded anyway: `metadata.isTest` / `isTestOrder` / `testMode` / top-level `testmode` are all checked and such an order is ignored entirely. If a real flag exists under another name, a test order would be counted as GMV under a deterministic key that could then never be reused |
| 22 | Webflow states a **zero-decimal** currency (JPY, KRW, VND, …) in that currency's OWN minor units, i.e. a ¥5,898 order is `value: 5898` — not in hundredths | **ASSUMED.** Row 10 is verified with a USD example; no Webflow order in a zero-decimal currency has been observed by this repo. Every sibling adapter in this repo multiplies by 10^decimals and the ledger convention is ISO minor units, so the two conventions genuinely differ here | If Webflow reports hundredths, every such order is recorded **100x inflated** — a ¥5,898 order as ¥589,800 — and nothing downstream can distinguish it, because `customerPaid` is the only source. The mapper logs a WARNING on the FIRST order per process per zero-decimal currency naming the store, the currency, the order id and the observed value, so the first one is visible rather than assumed away; `tests/test_webflow_event_adapter.py` pins that it fires for JPY and not for USD. Confirming the convention against one real merchant order closes this row |

## Residual gaps

* **No scheduler.** See above. It matters more here than for a platform with
  guaranteed delivery: Webflow webhooks are best-effort, so the sweep is the only
  recovery path and its latency is however often somebody invokes it.
* **The ordering assumption is bounded, not eliminated.** A resumed offset walk is
  safe under newest-first (it re-reads) and exact under oldest-first, but a list
  whose order is genuinely unstable between requests could let a resume skip a
  row. What exists is the overlap, the arming rule, the permanent disarm on any
  observed violation, and `ordering_verified` in every run's output. Closing it
  properly needs either a modified-since filter from Webflow or a full-history
  pass on a schedule.
* **Layer 2 is off unless a client secret is configured.** A deployment with no
  Webflow OAuth app authenticates deliveries with the URL secret alone: a 256-bit
  secret over TLS, but with no replay window and no rotation short of
  re-provisioning.
* **`ensure` rotation has a window.** `rotate=true` re-registers the URL; in-flight
  deliveries to the old one 401 until Webflow's retries or the sweep recover them.
* **A rotation killed between the persist and the registration is not
  self-healing.** "A crash between persist and register is harmless" holds on
  FIRST provisioning only, where nothing was working yet. On a rotation against a
  working store the live webhook still carries the OLD secret while the NEW one
  is already stored, so **every delivery 401s** until `ensure` is re-run — the
  store goes silent and only the sweep recovers it. A registration that FAILS
  (502, 504, `scope_required`) now restores the superseded secret and logs
  `action=rotation_rolled_back`, so the reachable half is closed; a process
  killed between the persist and that handler is not, and the repair is to
  re-run `ensure`.
* **The sweep's state write is a read-modify-write spanning I/O.** A run reads
  `reconciliation` once, spends many network calls walking Webflow, and writes at
  the end. The row lock in `merge_store_credentials` covers the WRITE only, so it
  is not what makes two replicas safe here. What does: the final write is a
  `mutate` that merges only the lanes THIS run walked into whatever
  `reconciliation` holds at write time (so a lane the run did not touch can never
  be clobbered), the per-process `_WEBFLOW_SWEEPS_IN_FLIGHT` set stops the
  same-process double-run, and a lost cursor for a lane both replicas *did* walk
  is benign in the only direction available: a cursor that goes backwards causes
  a **re-read**, and re-reading is free because the event ids are deterministic.
  It can never cause a skip.
* **No PSP visibility.** `customerPaid` is Webflow's own figure. A refund issued
  directly in Stripe, outside Webflow's commerce flow, is invisible to this bridge.
* **No refund identity, and no refund count.** One full-order refund per order,
  keyed `<orderId>:refund`. If partial refunds ever exist they would be
  under-counted — see assumption 18.
* **No Pivota order identity.** Nothing links a Webflow order back to a
  Pivota-originated purchase, so every Webflow order is platform-originated in the
  ledger. Closing that needs a structured marker at writeback, and there is no
  Webflow order writeback in this repo.
* **No catalogue.** `services/commerce_source_registry.py` claims no catalogue
  capability for Webflow, so a product sync reports an honest blocker rather than
  an empty success.
* **No app-uninstall handling.** Nothing deactivates a store whose token was
  revoked; the sweep starts failing loudly on the site check, which is the signal,
  but no automation acts on it.
* **The per-store `ensure` and `reconcile` guards are per-PROCESS.** They stop the
  operator double-click and the retry-on-timeout, not two replicas. The row lock
  in `merge_store_credentials` makes each individual merge atomic — which is what
  protects the `url_secret` against a concurrent cursor write — but it does not
  serialize a whole sweep, whose modify half is unbounded I/O. See the read-
  modify-write gap above for what actually bounds that.

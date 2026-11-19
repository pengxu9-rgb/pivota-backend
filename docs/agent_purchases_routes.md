# `/agent/v2/commerce/purchases`: the rail-neutral purchase door

`routes/agent_commerce_purchases.py` implements this contract. The storage is
`db/agent_purchase_ledger.py` (migration 264); the shared state vocabulary is
`services/payment_orchestration/rails.py`.

**Payment orchestration.** One purchase id (`pp_…`) and one state vocabulary across payment
rails: Reap and the A2A token rail. The reads (`GET`) open nothing, advance nothing and call no
rail. `POST` (below, its own dial) opens a purchase on the rail the router picks; it too calls no
rail (the rail's worker does that later). Purchases can still be created directly through
`POST /agent/v2/commerce/reap/purchases` (docs/reap_agentic_routes.md).

**Dark by default.** While `AGENT_PURCHASE_LEDGER_ENABLED` is not one of `1/true/on/yes`, every
request answers 404 `not_available`, whatever its shape. The dial is read per request.

## Authentication

The same two headers as the Reap routes: `X-API-Key` (the agent) and `X-Agent-User-JWT` (the
buyer). Missing buyer: 401 `agent_user_required`. Every read is scoped to the agent **and** the
buyer, in SQL; another owner's purchase and a purchase that does not exist are the same 404
`purchase_not_found`.

Errors arrive in the app-wide envelope; read the reason at `detail.error`.

## `GET /agent/v2/commerce/purchases/{purchase_id}`

`purchase_id` is a `pp_<24 hex>` id, or a rail's own id: Reap `rp_<24 hex>` or A2A `ap_<24 hex>` (a
rail purchase without a parent yet gets its `pp_` id on this first read). Any other shape is 404 `purchase_not_found`,
decided before any database read.

```json
{
  "purchase_id": "pp_…",
  "rail": "reap",
  "executor": "rail_managed",
  "rail_purchase_id": "rp_…",
  "state": "awaiting_buyer_authorization",
  "rail_state": "awaiting_approval",
  "created_at": "2026-10-10T11:00:00+00:00",
  "totals": {"currency": "USD", "quoted_total_minor": 4650, "…": "…"},
  "poll_after_seconds": 30,
  "next_action": {
    "type": "open_url",
    "kind": "approval",
    "url": "https://…",
    "expires_at": "2026-10-10T11:15:00+00:00"
  },
  "detail": {"…": "the rail's own owner-facing body, byte for byte"}
}
```

- `state`: one of `routing`, `needs_payment_method`, `locking`, `awaiting_buyer_authorization`,
  `placing`, `completed`, `failed`, `refused`, `expired`. The last four are terminal.
- `rail_state`: the rail's own word for the same state; for Reap, see docs/reap_agentic_routes.md.
- `next_action`: present only while the buyer must act on a rail-hosted page AND the rail route
  still offers a valid URL (`kind` is `card_binding` or `approval`). Absent means there is
  nowhere to send the buyer: poll again.
- `order_reference`: present on `completed`; the merchant order reference from the rail.
- `detail`: exactly what `GET /agent/v2/commerce/reap/purchases/{rp_id}` returns.
- 503 `state_unmapped`: the rail reported a state this service has no unified word for. Do not
  infer anything; retry later.
- 503 `ledger_unavailable`: the ledger could not be read. Says nothing about the purchase; retry.
- Reads do not depend on the Reap rail's own dial: like Reap's GET, a stored purchase stays
  readable while the rail is switched off.

## `GET /agent/internal/commerce/purchases/{purchase_id}` (server to server, AgenPas)

The gateway's read for the AgenPas page a buyer opens from a signed link (the buyer holds no agent key;
the gateway keeps no partner credentials). Dark until `AGENPAS_INTERNAL_READ_ENABLED` is on AT STARTUP:
off, the route is not registered, so every method and the OpenAPI schema look exactly like a path that
does not exist. Turning it on needs a restart.

- `X-Internal-Key` must equal `AGENPAS_INTERNAL_READ_KEY`, a key dedicated to this read (never the
  introspect key, `AGENT_AUTH_INTROSPECT_INTERNAL_KEY`). Missing or wrong: 401. Not configured on the
  server, or shorter than 32 characters: 503, never open.
- `X-Agenpas-Agent-Id` scopes the read by (agent, purchase id); another agent's purchase is the same 404
  `purchase_not_found` as no purchase. `X-Agenpas-Agent-User-Ref` is ignored (not the backend's user
  identity).
- The body is exactly the one `GET /agent/v2/commerce/purchases/{purchase_id}` returns for that purchase.
- A ledger or view fault: 503 `temporarily_unavailable`. Read-only. Not exempt from the rate limiter
  (only the introspect key is): page reads count as anonymous traffic and can 429 under a burst.

## `POST /agent/internal/commerce/purchases/{purchase_id}/shipping-address` (server to server, AgenPas)

The partner agent sends the buyer's shipping address; when it does not, AgenPas asks the buyer for
it. Same router as the read above: the same dial (`AGENPAS_INTERNAL_READ_ENABLED`, at startup), the same
key, the same (agent, purchase id) scoping; `X-Agenpas-Agent-User-Ref` is read and ignored.

- **Capture** is ONE predicate (`a2a_shipping_address.capture_enabled`), read by the door, the view and the
  worker alike: this dial AND the checkout step AND the token step. Only then does the worker wait for a
  missing contact, so only then does `POST /agent/v2/commerce/purchases` on the A2A rail accept a body
  without `buyer.email` and/or `buyer.shipping_address`, and only then does the view ask for it. One that is
  given is read by the Reap rules, plus, for the US, a real state or DC in `region` and a ZIP (12345 or
  12345-6789) in `postalCode` (`invalid_address`). Capture off (any of the three dials off, e.g. this read
  dial on before the checkout dial): both stay required and every answer is exactly as before. A request the
  router hands to Reap is parsed by Reap's own model, which still requires both.
- **Market.** The address's country; with no address, the one market of the purchase's own currency
  (`expected_currency`), never a default: none (or several) is 400 `shipping_market_unknown`. The A2A pilot
  is the US, in code: any other market on the A2A rail is 400 `shipping_country_unsupported`.
- **Waiting.** The purchase stays `opened` (unified `routing`), and the worker sends NOTHING for it, not even
  a card read, until its email and address are complete. The email is listed first: the card binding needs
  it. `last_error_code` is `shipping_address_required`, and the deadline is 30 minutes from creation
  (`a2a_rail_worker.SHIPPING_ADDRESS_SECONDS`). Then it is `expired` with `shipping_address_expired`, by the
  worker or the expiry sweep. There is no new state and no migration: the deadline sits in
  `buyer_action_expires_at`, with no `buyer_action_kind` / URL.
- **The view.** `next_action: {"kind": "shipping_address", "fields_missing": [...], "expires_at": ...}`. It
  has no `url` and no `prefill`; `expires_at` is absent until the worker has looked at the row. Field
  NAMES only (`email`, `firstName`, `lastName`, `phone`, `addressLine1`, `city`, `region`, `postalCode`,
  `country`), never a stored value. The same names sit in `detail.shipping_address_required`.
- **Body.** Flat JSON: `email, firstName, lastName, phone, addressLine1, addressLine2, city, region,
  postalCode, country`. A part the purchase already has may be left out (no `email` when `email` is not
  in `fields_missing`); a part that is sent is read whole. US only. Region is one of the 50 states or DC.
- **Write.** A compare-and-swap. It succeeds only while the purchase waits (`opened`, a part empty, the
  deadline not passed, the contact never purged) and fills only an empty part. The purchase is then due
  at once. A submit that lands while the worker holds the row is never undone: the worker's park then finds
  the contact complete and clears the code and the deadline, so the expiry sweep cannot end it.
- **Answers.**
  - 200: the unified view (as the read).
  - The same contact sent again: 200.
  - 409 `shipping_address_conflict`: the purchase no longer waits and holds something else, the
    email or address sent differs from the stored one, or it is not an A2A-rail purchase. The body is the
    error middleware's wrapper: `{"status": "error", "error": {"code": "CONFLICT", "message":
    "shipping_address_conflict", "details": {"error": "shipping_address_conflict"}, "documentation_url":
    ...}, "detail": {"error": "shipping_address_conflict"}, "metadata": {...}}`: read `detail.error`.
  - 422 `{"status": "error", "error": "invalid_shipping_address", "field_errors": {field: code}}`.
    The codes are `required`, `invalid`, `too_long` and `unsupported`; a submitted value is never
    echoed.
  - 404 `purchase_not_found`: another agent's purchase, or none.
  - 400 `agent_id_required`. 401. 503.
  - Nothing of the body is logged.

## `GET /agent/v2/commerce/purchases?limit=N`

The caller's purchases, newest first, each in the shape above. `limit` is 1..100 (default 20);
out of range is 400 `invalid_request`, never a silent clamp. The page is all-or-nothing: if any
purchase on it is in a state with no unified word, the whole list answers 503 `state_unmapped`
rather than leaving that purchase out (a missing row would read as a purchase that does not exist).

```json
{"purchases": [{"purchase_id": "pp_…", "…": "…"}], "limit": 20}
```

## `POST /agent/v2/commerce/purchases`

Opens a purchase on the rail `services/payment_orchestration/router.py` picks. **Dark**: unless BOTH
`PAYMENT_ORCHESTRATOR_ENABLED` and `AGENT_PURCHASE_LEDGER_ENABLED` are on, every request answers
404 `not_available`. Same authentication as the reads.

**Body**: the Reap create body (docs/reap_agentic_routes.md, `item_source`, `merchant_domain`,
`product_key`, `variant_key` (never the `<product_key>::canonical` placeholder: `409 row_variant_unverified`) or (cart link) `variant_id`, `quantity`, `buyer {email, shipping_address, consent_version}`,
`idempotency_key`, `expected_unit_price_minor` + `expected_currency`, `offer_code`, `return_url`)
plus an optional `payment_preference: {"rail": "reap" | "a2a"}`.

**Order of decisions**:
1. The request is normalised exactly as the Reap cart-link lane does it, and the **idempotency key
   is looked up first**, in the key table both rails share: a retry with the same key and body
   replays the ORIGINAL purchase on its ORIGINAL rail, whatever the router would answer today. A
   Reap original is answered by the Reap route; a different body is 409 `idempotency_conflict`, which
   names the key's purchase (`purchase_id`, `rail`) when it opened one, so the caller can read it.
2. A new attempt without the shown-money pair is 400 `invalid_request`, before the router.
3. The router:
   - **An explicit `payment_preference.rail` is never silently changed.** An `a2a` choice the A2A
     rail cannot take is 404 `not_available_on_this_rail` (the Reap route's wording for an
     unavailable rail; the reason is logged, never returned) and is never sent to Reap. `reap`
     goes to the Reap route.
   - **No choice**: the A2A rail when it may take the request (its dials, the agent's pilot
     membership, a cart link with no offer code, the (merchant, market) proven list, a cap for the
     currency), else the Reap route, unchanged.
   - **No fallback after routing.** A request routed to the A2A rail that the A2A create then
     refuses (pricing, the Tier B verdict, the amount cap, the buyer's details, ...) is REFUSED with
     that reason. It is never retried on Reap: the router decides once, before anything is written.
4. Reap: the request is handed to the Reap route, which answers in its own shape.
5. A2A: the Reap cart-link lane's checks (purchasability when enforced, an operator's disabled
   merchant row, the Tier B verdict, our catalog price and the shown-money check), the buyer's
   details by the Reap rail's own rules, the rail's per-currency cap, then our click and cart link,
   then the purchase and its key in one transaction (rolled back if another request wins the key).

**Response** (A2A): `202 {"purchase_id": "ap_…", "rail": "a2a", "state": "<unified>", "rail_state": "opened",
"poll_after_seconds": 5, "variant_title": …}`. Read it with the GET above.

**404 bodies differ on purpose**: `not_available` is this door being dark; `not_available_on_this_rail`
is the door being on and an explicit rail choice that cannot be honoured (the Reap route's word for an
unavailable rail). Neither names a reason, and neither opens anything.

## Operations

- Turn on, in this order:
  1. Backfill while the dial is still off (the table exists from the self-heal; the backfill is
     idempotent and safe beside live traffic): `python scripts/backfill_agent_purchases.py --dry-run`,
     then without `--dry-run`.
  2. Set `AGENT_PURCHASE_LEDGER_ENABLED=1`. From then on the Reap create route writes each new
     purchase's parent after its own commit (best-effort and bounded at 1 s on Postgres; a failure
     or a timeout is logged and never affects the purchase).
  3. Run the backfill once more for purchases opened between steps 1 and 2.
  Reads also heal: a single read heals that purchase, and a list read heals that owner's newest 100.
  Every heal goes row by row: a purchase whose parent cannot be written is logged and skipped, the
  rest are still healed, and that purchase is left alone (per process) for 10 minutes by the backfill
  and the event job, and for 30 seconds by list reads, before it is tried again. Five failures in a
  row end that heal run. The backfill script prints `failed=` per batch, carries on past failed rows,
  and exits 1 if any row failed. **Exit 1** means some Reap purchases still have no parent: the purchases
  themselves are untouched, but those rows are missing from the unified list until healed. Its last
  line is `done parented=… failed_attempts=… missing_parents=…`; the WARNING lines
  `reap parent heal failed purchase=rp_… error_type=…` name each one. Fix the cause, then run the
  backfill again until it exits 0 (`missing_parents=0`).
- Turn off: unset the dial. The table stays; nothing reads or writes it.

### Held A2A checkouts (operator path)

The A2A rail's checkout step HOLDS a purchase whenever it cannot prove what happened at the rail provider:
a setup or confirm whose answer nobody saw (`checkout_setup_unresolved`, `checkout_confirm_unresolved`),
a `paid` sent through the relay whose step was cut, a total it cannot verify, and the other codes in
`a2a_rail_worker._CHECKOUT_HOLD_CODES`. A held purchase stays in `paying` (the agent sees `placing`) for
good: no sweep ends `paying`, and **nothing ends a hold automatically** except one READ: for a hold whose
cause is an answer nobody saw (`a2a_rail_worker._CHECKOUT_LOOKUP_CODES`: setup / confirm / cancel unresolved,
the pending check, paying without a task, a prose / unrecognised / error-card confirm answer), the worker
asks the provider `checkout.lookup#submit` with our idempotency key (`pvt-<purchase id>-checkout`), read-only,
in its own conversation. The first lookup comes when the hold's own park ends, about 10 minutes after the
hold (`POLL_PARKED`); then again each time the time in `paying` has doubled, within 4 hours, and at most 9
times per purchase, ever: each one is counted BEFORE it is sent (`checkout_lookups`, migration 290), so a
crash in the middle of one still counts. An answer for the task the purchase recorded from ITS OWN confirm
answer (bound) goes through the ordinary result rules: completed only on a verified total in the row's
currency (a `currency` field naming another one is unverified), a held code stays held, a "not submitted"
fails it, an await goes back to polling. **N1 IS CLOSED BY "NO UNBOUND COMPLETION"**: the provider (11-19)
creates a checkout task only after our confirm, and a lookup answer echoes nothing we sent (no idempotency
key, checkout URL or merchant; its `card_last4` names the card, not the purchase), so an answer for a task
the purchase never recorded (unbound) cannot be verified as ours. It never completes the purchase, never
records an order id or a total, never adopts the task, never fails it and never makes it "not charged": the
hold stays, whatever it says. One that says the checkout succeeded or payment happened (`success: true`, or a
`result_status` / `status` that says paid, completed or payment-reached; a `state` only when it itself names a
payment, never `completed` alone, which is the task's lifecycle) moves the hold to
`checkout_payment_reached_unconfirmed`, which resolve-held refuses. Exact binding (an `idempotency_key`
echo) comes back only in its own review, once the provider confirms it. A result's status is read from
`status` or the documented `result_status` (both present and different: unrecognised, held); the fields
not read (`subtotal`, `shipping_fee`, `tax`, `errors`, `duration_ms`, `card_last4`) are ignored and never
stored. A money-result answer parks it for good (`money_result_seen`, never "not charged"); anything else
leaves it held. It never resends a setup or a confirm, and never looks up a
hold written after payment was reported. A 409 on the
confirm (the same key with another body: our own bug) is held and never looked up. Otherwise a human does:

1. List them (read-only, no PII): `python -m jobs.a2a_rail_operator list-held`. Each line is one purchase:
   its id, its hold code, whether a write is pending, whether a checkout task id is recorded.
2. **Check the provider's side for that purchase** (its order lookup, its dashboard, its support). Look for
   an order and for a charge on the purchase's token.
3. Only if there is **no order and no charge**: preview, then apply. In production run it as a one-off job
   (`scripts/ops/run_oneoff_job.sh`, behind the one-off gate):

       python -m jobs.a2a_rail_operator resolve-held --purchase-id ap_... --expected-code <the code from step 1>
       python -m jobs.a2a_rail_operator resolve-held --purchase-id ap_... --expected-code <code> --lookup
       python -m jobs.a2a_rail_operator resolve-held --purchase-id ap_... --expected-code <code> \
           --apply --operator <handle> --expect-env production --provider-checked

   `--lookup` (dry run only) adds ONE read-only lookup by our idempotency key and shows its safe fields only
   (`state`, `result_status`, `total`, `currency`, `task_id`; never `card_last4` or the order id), read at
   that moment and NEVER stored (not even as a lookup count). An answer for a task the purchase did not
   record from its own confirm answer is labelled "UNBOUND: this answer may belong to another purchase;
   not verified as ours": it is a hint for your check in step 2, never proof. An answer that reports payment
   (bound or not, read as above) makes the preview `eligible: false`, reason `payment_reported_by_lookup`:
   escalate, never resolve it here. Only a HELD row is looked up.

   `--apply` runs its OWN read-only lookup the same way (when the rail client is configured) and refuses
   (exit 3, `payment_reported_by_lookup`) on an answer that reports payment: what the preview saw is never
   stored, so the apply reads again. With no client configured, or a lookup that fails, the apply rests on
   your `--provider-checked` statement, as before. `--lookup` together with `--apply` is a usage error
   (exit 2): `--apply` always does its own. That lookup is a provider call too: the go for an `--apply` run
   covers it only when it says so.

   The purchase ends `failed` / `operator_confirmed_not_charged`. The write is a compare-and-swap on the
   code you read and refuses a row a worker holds right now, so a row that moved since is not touched
   (exit 3, with the reason).
4. If the provider DID place the order (or charged the token), do **not** resolve it here: it is not a
   failed purchase. Record the merchant's order number by hand with an engineer; there is no command for
   that yet.

`checkout.relay#paid` held as `checkout_setup_unresolved` after a cut step: the setup carrying the token
may or may not have gone out. If the provider shows no checkout session for the purchase at all, nothing
was set up, and the purchase can be resolved as above.

**Who may run it.** `list-held`, the preview (`resolve-held` without `--apply`) and `resolve-held --apply`
all read or write production, so each is a **production one-off**: run it through the one-off gate (`oneoff_gate.sh`,
GATE_OPEN first; the ops tooling, not this repo), **one job at a time**, and only with **the owner's go for that purchase**. The tool does not
check the gate itself.

**Never "not charged" over a recorded payment.** A hold written after the provider reported
`payment-reached` (`checkout_total_over_approved`, `checkout_total_unverified`,
`checkout_order_id_card_shaped`, `checkout_payment_no_order`, `checkout_payment_reached_unconfirmed`, and
`checkout_answer_scrubbed`, written when an answer that may follow a confirm echoed a card code (the confirm's
answer, a poll, a result, or a lookup, which may also run on a row whose confirm never went out); the
set is `a2a_rail_worker._CHECKOUT_PAYMENT_REACHED_CODES`; `checkout_payment_reached_unconfirmed` is also
written for a result status we do not know that speaks of payment or completion, e.g. `paid`, `completed`, never
for its negation such as `unpaid` or `payment_failed`; `checkout_order_id_card_shaped` is no longer written: an order id
that may echo the card or the token now COMPLETES without its reference, as `checkout_order_reference_withheld`) is refused, whatever the flags
(`payment_reached_recorded`): our own ledger says the order may be placed and paid. **Escalate; the merchant
refunds.**

**Not too soon.** A purchase must have been in `paying` for at least an hour
(`OPERATOR_MIN_HOLD_AGE_SECONDS`) before it can be resolved: the provider's staged checkout lapses after
about 30 minutes, and a check made sooner can miss an order that lands later. The write refuses a younger
row itself (`held_too_recently` in the preview).

**What a resolution causes.** The preview reads that purchase by id and shows its code, how long it has
been held, and whether the apply would be accepted. A resolved purchase keeps its token id: the next worker
tick's orphan pass voids that token on the rail (when a voider is configured), a second rail call caused by
the resolution.

**What it resolves, and what it does not.** `resolve-held` ends ONLY a checkout hold: a purchase in
`paying` with a token, held with one of the codes in `a2a_rail_worker._CHECKOUT_HOLD_CODES`, the code
alone (exit 3 otherwise). It does not resolve, and an operator must not try to resolve by other means:
- a VIC order hold (`vic_*` codes),
- `money_result_seen` (an answer reported money moving where none should have),
- a compound code (one carrying a schedule prefix, e.g. `paid_sent:...:...`, or `worker_error`).
For those, **escalate** to an engineer with the purchase id and the code. Never edit the database by hand:
the row's pending write and code are the only record of what may have happened at the provider.

**What the buyer sees.** A resolution usually lands after the buyer's 30-minute payment link has expired.
The buyer learns the outcome from a fresh read: the agent's `get_order` in the chat (the purchase body,
`state` `failed` with `last_error_code` `operator_confirmed_not_charged`), never from the old link.

## Webhook events (`purchase.*`)

Agents can be told about a purchase instead of polling. Events go through the existing agent
webhook system (`services/agent_webhook_service.py`): the same configuration (destination URL,
subscribed events), signing, retries and delivery log as the `order.*` events.

| Event | When | `data` |
|---|---|---|
| `purchase.action_required` | the buyer must open a rail-hosted page: `needs_payment_method` (`next_action.kind = card_binding`) or `awaiting_buyer_authorization` (`kind = approval`) | the unified purchase, with `next_action.url` |
| `purchase.completed` | the merchant order was placed | the unified purchase, with `order_reference` |
| `purchase.failed` | the purchase ended without an order: `failed`, `refused` or `expired` (`data.state` says which) | the unified purchase |

`data` is exactly the body of `GET /agent/v2/commerce/purchases/{id}` at the moment the event was
detected.

Envelope and signature, as for every agent webhook:

```
POST <destination_url>
X-Pivota-Event: purchase.completed
X-Pivota-Delivery: whd_…
X-Pivota-Timestamp: <unix seconds>
X-Pivota-Signature: v1=<hex HMAC-SHA256(signing_secret, "<timestamp>.<raw body>")>

{"id": "evt_…", "type": "purchase.completed", "created_at": "…", "data": {…}}
```

Receiver rules:

- **De-duplicate on `id`.** Every delivery and every retry of one event carries the same `id`.
  Delivery is at-least-once.
- **Do not rely on order.** Each event carries the purchase as of its own state (`data.state`); a
  retried delivery can arrive after a newer one. `GET /agent/v2/commerce/purchases/{id}` is always
  the authoritative read.
- Answer 2xx quickly. 408, 429 and 5xx are retried (60 s, 5 min, 30 min, 2 h, 12 h); other
  statuses are not.

Subscribing: `purchase.*` events are in the webhook catalog. New webhook configurations receive them
by default. **An existing configuration with an explicit event list must add them**, and an event
that happens while the agent is not subscribed (or has no destination) is recorded as `skipped` and
is not sent later: an event is about the moment it happened, and `GET` answers for any purchase the
agent asks about afterwards.

Operations: events are detected and handed to the webhook sender by the worker job
`agent_purchase_events` (every `AGENT_PURCHASE_EVENTS_INTERVAL_SECONDS`, default 30). It is dark
unless `AGENT_PURCHASE_WEBHOOKS_ENABLED` and `AGENT_PURCHASE_LEDGER_ENABLED` are both on **on the
worker service** (the job runs there; the ledger flag on `web` alone does not arm it).

- Events are written to `agent_purchase_events` (migration 265) before anything is sent.
- Both rails are detected, Reap and the A2A token rail, by the same rules. An A2A purchase whose
  parent row the create route did not write (it is written best effort after the purchase commits)
  is given its parent by the job before it reads, up to 50 per run, so it is not missed. The heal
  uses the same window as the read below (state entered within the lookback), whenever the purchase
  was created. The job's counts include `a2a_parent_heal_failed`, `a2a_parent_heal_deferred` (rows
  read in that run that were skipped, waiting to retry after a failure) and `a2a_parent_heal_stopped`
  (the run gave up after failures in a row).
- Only purchases that entered their current state within `AGENT_PURCHASE_EVENTS_LOOKBACK_HOURS`
  (default 72, max 168) are considered, so switching the dial on does not replay old purchases;
  it DOES send events for purchases that changed state within that window. For a quiet first
  arming, set the lookback to 1 for the first run.
  **A pause or outage longer than the lookback drops the events of purchases that changed state
  during it**; they stay readable with `GET`.
- An action state is only an event while its page is attached and live. A purchase whose page is
  refused is recorded `skipped` (`no_action`) and re-checked every 5 minutes, so a page the rail
  re-publishes in the same state still produces the event. An owner mismatch or an unmapped state
  is recorded `skipped` once, finally.
- When webhook delivery is switched off (`WEBHOOK_DELIVERY_ENABLED`, which outside production is off
  by default), nothing is claimed: events stay `pending` and go out when delivery is on again.
- `status = handed_over` means the sender accepted the event; `delivery_status` is its first
  delivery's outcome (`delivered`, `retrying`, `failed`). A non-retryable answer (for example 404)
  ends the event as `handed_over` / `failed`: watch `delivery_status`, and the agent delivery log.
- An event the sender raised on ten times is `abandoned` (kept, visible). The job can be paused and
  run on demand from the admin scheduler routes.

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
`product_key`, `variant_key`, `quantity`, `buyer {email, shipping_address, consent_version}`,
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
- Turn off: unset the dial. The table stays; nothing reads or writes it.

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

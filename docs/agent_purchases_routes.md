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
  nowhere to send the buyer: poll again. With the AgenPas consent gate on (below) and no consent yet
  for that step, it is `{"kind", "expires_at", "consent_required": true}` with NO `url` (and no
  `type`): the buyer must accept Pivota's terms on the AgenPas page first.
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
- A ledger or view fault: 503 `temporarily_unavailable`. The read writes nothing. Not exempt from the rate
  limiter (only the introspect key is): page reads count as anonymous traffic and can 429 under a burst.
- With the consent gate on (below), the body is gated like every surface and, in `needs_payment_method`,
  `locking` and `awaiting_buyer_authorization`, carries a `consent` block (gate off: no block, the body is
  byte for byte the public one):

```json
"consent": {
  "required": true, "step": "approval", "satisfied": false, "available": true, "min_dwell_seconds": 3,
  "terms_version": "…", "privacy_version": "…", "terms_url": "https://…", "privacy_url": "https://…",
  "offer": {"id": "<64 hex>", "issued_at": "2026-11-18T12:00:00Z"}
}
```

  `offer.id` is 64 hex characters; `offer.issued_at` is ISO-8601 UTC to the second ending in `Z`, and must be
  echoed byte for byte. `min_dwell_seconds` (0..120) is how long after `issued_at` the write accepts it.
  `available` is false (and there is no `offer`) when the terms / privacy URL or version, or the offer
  secret, is not configured, when the consent write is off, or when the consent table could not be read:
  the page then says "not available right now", and the hosted page stays withheld. The versions and URLs
  appear only when configured; `offer` only while available and not yet satisfied. The read writes nothing:
  the offer is stateless (an HMAC, below).

## `POST /agent/internal/commerce/purchases/{purchase_id}/consents` (server to server, AgenPas)

Records that the holder of the AgenPas link accepted Pivota's terms of service and privacy policy on
Pivota's page, before the rail's card page (`card_binding`) or approval page (`approval`). A payment
partner requires that acceptance before a card is stored and before every approval, recorded by us.

- **Dark** until `AGENPAS_CONSENT_WRITE_ENABLED` is on AT STARTUP (like the read's dial: off, the route is
  not registered and the OpenAPI schema is unchanged; turned off without a restart, it answers the
  unknown-route 404).
- **Its own key**: `X-Internal-Key` must equal `AGENPAS_INTERNAL_CONSENT_KEY` (>= 32 characters, and not
  the read key; the read key stays read-only). Missing / short / equal to the read key on the server: 503
  `internal_key_not_configured`. Missing or wrong in the request (the read key included): 401.
- `X-Agenpas-Agent-Id` scopes it as for the read. `purchase_id` may be the `pp_` id or the rail id; the
  consent is keyed on the RAIL id and the rail row is read agent-scoped (the parent is healed best effort).
- Body, exactly (strings, at most 1 KiB): the offer the read showed.

```json
{"step": "approval", "offer_id": "<offer.id>", "issued_at": "<offer.issued_at>",
 "terms_version": "<consent.terms_version>", "privacy_version": "<consent.privacy_version>"}
```

| Answer | When |
|---|---|
| 201 `{"created": true, "consent": {step, approval_epoch, terms_version, privacy_version, evidence, accepted_at}}` | recorded |
| 200 `{"created": false, "consent": {…}}` | already recorded for this step (a double click is one row) |
| 400 `consent_refused` | not exactly that body, or the offer's HMAC does not verify for this purchase, step, approval and the current terms |
| 404 `purchase_not_found` | no such purchase for this agent |
| 409 `consent_stale` | the versions are not the configured ones, the purchase is final or not in that step, or the offer is older than 1800 s: re-read and show the fresh offer |
| 409 `consent_too_soon` | the offer is younger than `consent.min_dwell_seconds` (`AGENPAS_CONSENT_MIN_DWELL_SECONDS`, default 3) |
| 503 `consent_unavailable` | terms / privacy configuration or offer secret missing |
| 503 `temporarily_unavailable` | a ledger fault |

Any refusal writes nothing. The write calls no rail.

**A version change after a consent does not ask again.** A consent covers its step as it was offered: once
recorded for (rail purchase, step, approval epoch), that step is satisfied even if `AGENPAS_TERMS_VERSION`
or `AGENPAS_PRIVACY_VERSION` changes later in the same purchase. A re-POST with the new versions (and an
offer minted under them) answers 200 `created: false`, echoing the STORED row's versions, not the new ones.
The next step (or the next purchase) is offered under the new versions.

**Reap's contact re-entry.** While a Reap purchase asks the agent for the buyer's contact again
(`detail.contact_reentry_required`), the read's block is `available: false` with no offer, and a POST
answers 409 `consent_stale`.

**`available` is true only when the POST can succeed now**: the write route is registered (its dial was on
at startup), `AGENPAS_CONSENT_WRITE_ENABLED` is still on, `AGENPAS_INTERNAL_CONSENT_KEY` is valid (>= 32
characters, not the read key), the configuration and offer secret are set, and the consent table could be
read.

**What the record proves, and what it does not.** `evidence = link_holder_on_pivota_page`: whoever held
the AgenPas link ticked "I agree" on Pivota's page. The agent holds that link too. The dwell, and the
gateway's `Origin` / `Sec-Fetch-Site` / JSON checks, stop naive automation only; they do not prove a
human. The row holds no PII (no IP, no user agent, no email).

## The AgenPas consent gate (`AGENPAS_CONSENT_GATE`)

Default off, read per request. Off, every body above is byte for byte what it was (golden-tested).

On, for a step that needs a consent (`card_binding` while the buyer binds a card, `approval` while they
approve, and, on the A2A rail, `shipping_address` while the purchase waits for the buyer's address: its form
is withheld the same way, see the shipping-address write) and while none is recorded for (rail, rail purchase, step, approval epoch), **no surface carries
the rail's hosted URL**: this door's read and list, the Reap rail's own routes (GET, list, create, resume,
recover), the `purchase.*` webhook payloads, and the internal AgenPas read. The rail's `detail` drops
`hosted_url`, keeps its deadline (`hosted_url_expires_at`, `approval_deadline`) and adds
`buyer_step = {"kind", "expires_at", "consent_required": true, "available"}` (`available` on live reads only);
`next_action` becomes
`{"kind", "expires_at", "consent_required": true}`. `purchase.action_required` still fires, with that
body. Once the consent is recorded, every surface answers today's body (URL included). The A2A rail's 3DS
page (`authenticate`, while `placing`) is never gated. A consent table that cannot be read withholds
every page (fail closed). List pages read the consents once per page.

`buyer_step.available` follows the same rule as the internal read's `consent.available`: true only when the
consent POST could succeed now (configuration and offer secret set, the write registered, its dial on and its
key valid, the consent table readable) and the step is not blocked by Reap's contact re-entry. **It is only
meaningful on a live read** (this door's GET and list, the Reap routes, the internal AgenPas read), where the
serving process computes it for its own routes. Web and the worker run the same app (`main:app`), each with
its own boot-time dials, so a value computed on the worker could disagree with web's: `purchase.*` webhook
payloads therefore never carry `buyer_step.available` (it is dropped when an event is recorded and again on
every hand-over, retry and listing, so an older stored payload goes out without it too). Read the purchase
for the live value. Set `AGENPAS_CONSENT_WRITE_ENABLED`, `AGENPAS_INTERNAL_CONSENT_KEY` and the rest of the
configuration identically on web and the worker anyway.

**The offer** binds the agent id, the rail, the rail purchase id, the step, the approval epoch, both versions,
both URLs and `issued_at` (length-prefixed). **Rotating `AGENPAS_CONSENT_OFFER_SECRET`**: set the old value
as `AGENPAS_CONSENT_OFFER_SECRET_PREVIOUS` for at least 30 minutes (the offer's lifetime). An offer minted
under it answers 409 `consent_stale` (the page re-reads and shows a fresh offer), not 400. Without it, offers
in flight answer 400 `consent_refused`. The previous secret verifies only; nothing is minted with it.

The approval consent may be given while the rail is still pricing (`locking`) or on the approval page,
and binds to that purchase's single approval (`approval_epoch` 1 on both rails: a Reap re-quote fails the
purchase, and the A2A ledger has no re-quote edge: `awaiting_authorization -> quoting` is refused; a test
fails if it is re-added or a raw SQL writer appears, and re-adding it needs an approval epoch on the consents).

Configuration (all on the backend; none is read by the browser):

| Env | |
|---|---|
| `AGENPAS_CONSENT_GATE` | the gate, default off. **Set it on BOTH the `web` and the `worker` service** (the worker builds and sends the `purchase.*` webhooks); each logs its state once at boot (WARNING `purchase_consent: service=… AGENPAS_CONSENT_GATE=on|off …`) |
| `AGENPAS_CONSENT_WRITE_ENABLED` | the write route, default off, read at startup |
| `AGENPAS_INTERNAL_CONSENT_KEY` | the write's key, >= 32 characters, not the read key |
| `AGENPAS_TERMS_URL`, `AGENPAS_TERMS_VERSION` | Pivota's buyer terms of service: https URL, version tag (printable ASCII, <= 32) |
| `AGENPAS_PRIVACY_URL`, `AGENPAS_PRIVACY_VERSION` | Pivota's buyer privacy policy: the same shapes |
| `AGENPAS_CONSENT_OFFER_SECRET` | the offer HMAC key, >= 32 characters, backend only, and neither the read key nor the consent key (else: unconfigured, `available` false) |
| `AGENPAS_CONSENT_OFFER_SECRET_PREVIOUS` | optional: the secret before the last rotation, verification only (same rules) |
| `AGENPAS_CONSENT_MIN_DWELL_SECONDS` | a whole number 0..120, default 3 (anything else: 3); stated to the page as `consent.min_dwell_seconds` |

**Turn on, in this order**: the gateway's consent UI and rails first (`AGENPAS_CONSENT_ENABLED`,
`AGENPAS_RAILS`), then the backend configuration above and `AGENPAS_CONSENT_WRITE_ENABLED` (a restart),
then `AGENPAS_CONSENT_GATE` on the **worker FIRST, then on `web`** (worker on and web off leaks nothing new;
web on and worker off sends every card and approval URL to agents in webhooks). Check both boot lines.
**Deliveries made before the gate went on** still hold the hosted URL in their stored payload (the
`purchase.*` event row and the agent webhook delivery row): every send re-applies the gate in the process
that sends, at send time: the first hand-over, the retry loop, and a manual retry, and the deliveries list
(the agent webhook deliveries list, `routes/agent_webhooks.py`) shows them gated too. While no consent exists for
the step, the payload goes out without the URL and with `next_action.consent_required`, so a pending
`purchase.action_required` backlog never carries a card or approval URL past the gate. The event rows are not
rewritten (a delivery row records what each attempt actually sent). **Once an attempt has sent the gated body, that
delivery stays gated**: its row now holds the body without the URL, and a later retry, even after the consent
is recorded, re-sends that body (the gate only withholds a URL; it never adds one back). An agent that gets a
gated `purchase.action_required` reads the purchase (`GET /agent/v2/commerce/purchases/{id}`) after the
buyer has agreed on the AgenPas page; that live read carries the hosted URL. A retry made after the consent
from a row that still holds the URL sends it as before. Gate on before the rest: every card / approval page is withheld and the
AgenPas page says "not available right now". **Precondition for scripts/ops/reap_local_e2e.py and the
Reap staging preflight**: the gate is off (they open the hosted page directly), or the run records the
consent step first.

**Turn off**: unset `AGENPAS_CONSENT_GATE` on **`web` first, then the worker**; bodies are today's again at
once. The records stay
(insert-only: nothing updates or deletes them, no PII scrub touches them).

## `POST /agent/internal/commerce/purchases/{purchase_id}/shipping-address` (server to server, AgenPas)

The partner agent sends the buyer's shipping address; when it does not, AgenPas asks the buyer for
it. Same router as the read above: the same dial (`AGENPAS_INTERNAL_READ_ENABLED`, at startup), the same
key, the same (agent, purchase id) scoping; `X-Agenpas-Agent-User-Ref` is read and ignored.

- **Capture** is ONE predicate (`a2a_shipping_address.capture_enabled`), read by the door, the view, this
  write and the worker alike. It is on only when ALL of these are:
  - `A2A_SHIPPING_CAPTURE_ENABLED`, capture's own dial, default off;
  - this read dial (`AGENPAS_INTERNAL_READ_ENABLED`);
  - the checkout step (`A2A_RAIL_CHECKOUT_STEP_ENABLED`) and the token step (`A2A_RAIL_TOKEN_STEP_ENABLED`);
  - the AgenPas consent gate (`AGENPAS_CONSENT_GATE`: no address is ever asked for without it), AND a consent
    that can actually be given: the terms / privacy configuration and offer secret valid,
    `AGENPAS_CONSENT_WRITE_ENABLED` on and `AGENPAS_INTERNAL_CONSENT_KEY` valid
    (`purchase_consent.consent_can_be_given`).

  Every one is an env value, so the predicate gives the SAME answer in every process that has the same env
  (web and worker). It deliberately does not ask whether this process mounted the consent write's router
  (`purchase_consent.write_ready`'s mount record): only `web` mounts it, so the worker would answer
  differently from web for the same row. Set the same values on both services.
  Only with capture on does the worker wait for a missing contact, so only then does
  `POST /agent/v2/commerce/purchases` on the A2A rail accept a body without `buyer.email` and/or
  `buyer.shipping_address`, and only then does the view ask for it. One that is given is read by the Reap
  rules, plus, for the US, a real state or DC in `region` and a ZIP (12345 or 12345-6789) in `postalCode`
  (`invalid_address`). Capture off (any one of them off, e.g. this read dial on before the checkout dial):
  both stay required and every answer is exactly as before. A request the router hands to Reap is parsed by
  Reap's own model, which still requires both.
- **Turn capture on, in this order**: the read dial, the checkout step and the token step (each per its own
  runbook), then the consent gate and its configuration (the section above), and
  `A2A_SHIPPING_CAPTURE_ENABLED` LAST: on the **worker FIRST, then `web`** (with web on and the worker off,
  the door opens purchases without a contact that the worker ends at once, `shipping_address_not_collected`).
  **Turn it off** with `A2A_SHIPPING_CAPTURE_ENABLED` on **`web` first** (no new purchase opens without its
  contact), then the worker. **Check both boot lines** (WARNING `purchase_consent: service=…`): each carries
  `A2A_SHIPPING_CAPTURE_ENABLED=on|off`, `consent_givable=yes|no` and `shipping_capture=on|off`, and web and
  the worker must agree. A worker at `shipping_capture=off` (its consent key or offer secret missing, say)
  while web is on ends every waiting purchase at its next poll.
- **What happens to waiting purchases when capture goes off** (any of the dials above, the consent gate's
  rollback included), as the code does it:
  - the worker, at the row's next poll (a parked row is polled every 60 s,
    `a2a_rail_worker.POLL_SHIPPING_ADDRESS`), ends an A2A purchase that is `opened` with its email or its
    address ABSENT: `expired`, `last_error_code` `shipping_address_not_collected`, before any rail call (no
    card read, no binding) and with the contact dropped like every terminal write
    (`a2a_rail_worker._contact_never_coming`). Only the door with capture on opens such a row, so nothing
    the capture-off door took (both parts present) is touched;
  - the view stops showing the form and the shipping step at once (the same predicate), the consent POST
    answers 409 `consent_stale` for the step (404 where its own dial is off), and this write answers 409
    `shipping_address_not_collected` (404 with the read dial off: the router is dark);
  - a recorded consent stays (insert-only). Turning capture back on does not revive an ended purchase: the
    agent opens a new one.
- **Consent first.** The form is a consent-gated step (`shipping_address`, A2A only, approval epoch 0; the
  owner's decision of 11-20). Until the buyer accepted the terms and privacy policy on the AgenPas page, no
  surface carries the field list: `detail.shipping_address_required` is withheld, `detail.buyer_step` is
  `{"kind": "shipping_address", "expires_at", "consent_required": true, "available"}` and `next_action` is
  `{"kind": "shipping_address", "consent_required": true, "expires_at"}` (no `fields_missing`). The internal
  read's `consent` block is offered for the step, the consent POST accepts `"step": "shipping_address"` (only
  for an A2A purchase that waits for its contact; otherwise 409 `consent_stale`), and once it is recorded the
  form appears as above. The write below answers 409 `shipping_address_not_collected` while capture is off
  and 409 `consent_required` before the consent (nothing written in either case; both checks run before the
  body is read), and 503 when the consent table cannot be read. Stored `purchase.*` payloads that still carry the form are
  re-gated when they are sent, retried or listed. A terms / privacy version bump does not re-ask.
- **Market.** The address's country; with no address, the one market of the purchase's own currency
  (`expected_currency`), never a default: none (or several) is 400 `shipping_market_unknown`. The A2A pilot
  is the US, in code: any other market on the A2A rail is 400 `shipping_country_unsupported`.
- **Waiting.** The purchase stays `opened` (unified `routing`), and the worker sends NOTHING for it, not even
  a card read, until its email and address are complete. The email is listed first: the card binding needs
  it. `last_error_code` is `shipping_address_required`, and the deadline is 30 minutes from creation
  (`a2a_rail_worker.SHIPPING_ADDRESS_SECONDS`). Then it is `expired` with `shipping_address_expired`, by the
  worker or the expiry sweep. "Waits" is ONE rule (`a2a_shipping_address.awaits_contact`) for the read (the
  form and the consent offer) and the consent POST: `opened`, a part missing field by field
  (`fields_missing`), never purged, the deadline not passed, capture on. Past the deadline the read offers
  nothing and the consent POST answers 409 `consent_stale`, even before the worker or the sweep ends it. There is no new state and no migration: the deadline sits in
  `buyer_action_expires_at`, with no `buyer_action_kind` / URL.
- **The view.** `next_action: {"kind": "shipping_address", "fields_missing": [...], "expires_at": ...}`. It
  has no `url` and no `prefill`; `expires_at` is absent until the worker has looked at the row. Field
  NAMES only (`email`, `firstName`, `lastName`, `phone`, `addressLine1`, `city`, `region`, `postalCode`,
  `country`), never a stored value. The same names sit in `detail.shipping_address_required`.
- **Body.** Flat JSON: `email, firstName, lastName, phone, addressLine1, addressLine2, city, region,
  postalCode, country`. A part the purchase already has may be left out (no `email` when `email` is not
  in `fields_missing`); a part that is sent is read whole. US only. Region is one of the 50 states or DC.
- **Write.** A compare-and-swap. It succeeds only while the purchase waits (`opened`, a part empty, the
  deadline not passed, the contact never purged) and fills only an empty part. "Empty" is the waiting
  rule's: a stored address that lacks a required field counts as absent, so the form asks for the whole
  address and the write replaces exactly that stored value (it compares on it, so an address a concurrent
  write completed is never overwritten). A complete stored address is never replaced. The purchase is then due
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
  - 409 `shipping_address_not_collected`: capture is off (any of its dials, see above); nothing is
    written and the body is not read. 409 `consent_required`: no consent to the step yet. Both are wrapped
    like the conflict above: read `detail.error`.
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
`--lookup` also sends a provider call (one read-only checkout lookup), so it is a production one-off under the
same rule; the go for the preview does not cover it unless it names --lookup.

**Never "not charged" over a recorded payment.** A hold written after the provider reported
`payment-reached` (`checkout_total_over_approved`, `checkout_total_unverified`,
`checkout_order_id_card_shaped`, `checkout_payment_no_order`, `checkout_payment_reached_unconfirmed`,
`checkout_payment_reached_declined` (the measured decline, below), and `checkout_answer_scrubbed`, written when an answer that may follow a confirm echoed a card code (the confirm's
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

### A provider-confirmed decline (`close-declined`)

**The hold.** The worker writes `checkout_payment_reached_declined` for exactly one answer: status
`payment-reached`, `success` EXACTLY `false`, and no order id in the RAW answer (the `order_id` key absent, or
`null`, or exactly `""`; `a2a_rail_worker._raw_order_id_absent`). Anything else stays
`checkout_payment_reached_unconfirmed`: `success` missing, or an order id of any other shape (a whitespace
string, `"#"`, `0`, a float, an object, a list), since a malformed order id may still be an order. It is a
payment-reached hold like the others: `resolve-held` refuses it, and the worker never looks it up.

**The close.** After the provider confirms **in writing** that this purchase was declined (no authorization
approved or captured, and no order at the merchant), a human ends it as `failed` /
`operator_confirmed_declined`:

       python -m jobs.a2a_rail_operator close-declined --purchase-id ap_... \
           --evidence-reference <opaque handle> --evidence-file-sha256 <sha256 of the statement file> \
           --evidence-observed-at <when it was sent, ISO with offset> \
           --provider-reference-kind checkout_task_id|external_order_reference --provider-reference <value> \
           --attest-no-authorization --attest-no-merchant-order [--lookup]
       # then, with the fence the preview printed:
           ... --apply --operator <handle> --expect-env <PIVOTA_ENV> --expected-code <code> \
           --expected-state-entered-at <iso> --expected-token-digest16 <hex> --expected-task-digest16 <hex> \
           --expected-pending-write <w|none>

- **Off by default:** `A2A_RAIL_OPERATOR_CLOSE_DECLINED_ENABLED` on the one-off job's env only. When it's off,
  the command refuses before any read. It also refuses while the audit table or its append-only trigger is
  missing. Migration 296 is their only builder: it has no startup heal, and it's applied once, as a gated
  one-off after the merge.
- **Who:** a production one-off, one at a time, with the owner's go for THAT purchase. The go names the
  statement's reference and whether `--lookup` runs. Preview first: it prints the fence (the code,
  `state_entered_at`, 16-hex sha256 prefixes of the token id and of the task id, whether a task is recorded,
  the pending write) and never either id. Given the evidence, it also says whether the apply would be accepted.
- **The evidence:**
  - The statement must name one of OUR ids: the checkout task id, or the external order reference we sent
    with the token. It must equal the purchase's own value.
  - It must be dated at least `OPERATOR_MIN_HOLD_AGE_SECONDS` after the hold began (after the staged checkout
    lapsed), no later than now plus 5 s, and no more than 7 days ago.
  - Only the reference, the file's hash and the facts above are recorded, append-only, in
    `a2a_rail_manual_resolution_audit`, with a digest computed on the server. Never the statement's text,
    card data, the buyer's contact or the token id.
- **The fence:** the code, `state_entered_at`, the token and task digests, and the pending write. Every
  `--apply` guard runs before anything is read or sent, so a wrong `--expect-env` costs no provider call. Not
  `updated_at`, because the worker claims and releases a held row every `POLL_PARKED`; not `claimed_by`,
  because that claim never moves the row, and its release writes nothing once the close clears it. On
  Postgres the transition and the audit row are ONE statement.
- **`--lookup`:** ONE read-only lookup (a provider call, so the go must name it). It must be BOUND (the
  task the purchase recorded from its own confirm answer), with `success` false, a decline status and no
  order id (`a2a_rail_worker.answer_is_confirmed_decline`). Anything else refuses. A row held under the
  legacy `checkout_payment_reached_unconfirmed` closes only with `--legacy-unconfirmed`, which makes that
  lookup mandatory. That needs `A2A_RAIL_CHECKOUT_STEP_ENABLED` in the one-off's env; the lookup doesn't count
  toward `checkout_lookups`. A legacy row with no recorded task is refused.
- **A replay** with the same evidence and operator answers `already_resolved` and writes nothing; anything
  else on a closed purchase is refused.
- **Never:** a retry, a setup, a confirm, a cancel, a new token, a refund claim, `completed`, an order id or a
  total, another code, state or rail, or a batch.

**What a close causes.**
- **The agent:** `get_order` shows `failed` / `operator_confirmed_declined`, and `purchase.failed` fires
  ("ended without an order"). The agent may offer to buy again, which is right only because the decline is
  confirmed in writing.
- **The token:** it stays OFF the orphan list for 72 hours after `terminal_at`
  (`ORPHAN_DECLINED_HOLD_SECONDS`), so a late authorization or dispute can still be matched to it first.

**If a late order or a reversed authorization appears after a close.** `failed` is terminal. Write an
incident note with the purchase id, the merchant's order number or the authorization reference, and the time.
The owner then contacts the merchant and the provider (cancel or refund at the merchant). Nothing is written to
the ledger by hand; the audit row stays as the record of what was believed and why.

### Rail scorecard (read-only)

`python -m jobs.purchase_rail_scorecard [--days 14] [--min-sample 30] [--exclude-agent-id <id> ...]`
prints one JSON object: per rail, merchant domain, market and new or returning buyer, how many
purchases were routed, how they ended (completed, failed, refused, expired, held, in flight), the
success rate (completed over finished; a held purchase counts against the rail) and the named codes
that ended the rest. It also counts how those purchases came to their rail, from the parent's stored
routing plan. It only reads (three SELECTs), calls no rail, changes no routing, and prints no buyer,
agent or purchase identifier. In production it runs as a one-off job behind the one-off gate. Test
agents are not removed by themselves: pass each as `--exclude-agent-id`. The module docstring defines
every number and states the limits.

## Webhook events (`purchase.*`)

Agents can be told about a purchase instead of polling. Events go through the existing agent
webhook system (`services/agent_webhook_service.py`): the same configuration (destination URL,
subscribed events), signing, retries and delivery log as the `order.*` events.

| Event | When | `data` |
|---|---|---|
| `purchase.action_required` | the buyer must open a rail-hosted page: `needs_payment_method` (`next_action.kind = card_binding`) or `awaiting_buyer_authorization` (`kind = approval`) | the unified purchase, with `next_action.url` (with the consent gate on and no consent yet: `next_action.consent_required`, no url) |
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

# Wix commerce telemetry

Native Wix eCommerce order and transaction events into the canonical commerce
ledger, through `POST /webhooks/wix`.

Everything below was verified against the Wix documentation on 2026-09-04. The
handful of things the docs do **not** state are called out as UNVERIFIED rather
than guessed at.

---

## Why this bridge is shaped differently from every sibling

### 1. Webhooks are an APP extension, not a per-store subscription

Wix webhooks are configured once, per app, in the app dashboard: pick an API
category, pick an event, give **one** callback URL, add permissions, Subscribe
([Handle Webhook Events without the JavaScript SDK][no-sdk]). There is no REST
call that registers a webhook for one site, so:

* there is **no subscription manager** in this bridge (BigCommerce has
  `services/bigcommerce_webhook_subscriptions.py`; Wix needs no equivalent);
* the route is **static** — `POST /webhooks/wix`, no store id in the path —
  because every site that installed the Pivota app delivers to that same URL;
* the store is resolved from the delivery's `instanceId`, "the unique
  identifier of your app within the site" ([About Webhooks][about]).

Adding a webhook creates a new **minor** version of the app, which is pushed to
sites on the latest major version automatically; sites on an older major version
will not deliver the new event until the owner updates ([About Webhooks][about]).

### 2. The body IS a JWT

There is no HMAC header. The entire POST body is a JSON Web Token signed by Wix
and verified with the app's **public key** ([About Webhooks][about]). The
reference handler is the exact contract this bridge implements:

```js
const rawPayload = jwt.verify(request.body, PUBLIC_KEY);
event     = JSON.parse(rawPayload.data);   // outer claim, a JSON STRING
eventData = JSON.parse(event.data);        // inner data, a JSON STRING AGAIN
```

Two parses, not one. The outer claim carries `eventType`, `instanceId`, `data`
(JSON string) and `identity` (JSON string) ([About the Structure of
Webhooks][structure]).

**UNVERIFIED:** the docs never name the signing algorithm and never show the
JWT header or its registered claims. `services/wix_webhook_auth.py` pins
**RS256** — that is what "verify with your public key" means for an RSA PEM and
what `jsonwebtoken`'s `jwt.verify` accepts for one — and refuses anything else
rather than trusting the token's own `alg`. For the same reason `exp` is
enforced only when the token carries one: requiring a claim the docs do not
promise would refuse every real delivery. An expired token is always refused.

### 3. Full entities arrive for orders; transactions need one read-back

| Event | Order entity in the payload? |
|---|---|
| `order` / `created`, `updated`, `approved`, `canceled`, `payment_status_updated` | **Yes** — no fetch |
| `order_transactions` / `refund_completed`, `details_updated` | **No** — one read-back |

An Order Transactions body is `{orderId, refund, sideEffects,
orderTransactions}`, and a Wix `Price` is `{amount, formattedAmount}` — there is
**no currency anywhere in it**. `merchant_commerce_event_funnel_service` drops
any money row whose currency is empty, so a refund mapped from that payload
alone would be invisible in the funnel. `services/wix_order_fetch.py` therefore
reads `GET /ecom/v1/orders/{orderId}` with the store's stored credential for
those two events only; a failure answers **503**, never a silent 200, because
Wix retries a non-2xx up to 12 more times over ~48 hours ([About
Webhooks][about]) and a 200 would drop the refund for good.

---

## Setup

### In the Wix app dashboard

1. **Webhooks → + Create Webhook.** API Category **eCommerce**. Create one
   subscription per event below, all pointing at the same callback URL:

   ```
   https://api.pivota.cc/webhooks/wix
   ```

   | Wix event | `entityFqdn` / `slug` |
   |---|---|
   | Order Created | `wix.ecom.v1.order` / `created` |
   | Order Updated | `wix.ecom.v1.order` / `updated` |
   | Order Approved | `wix.ecom.v1.order` / `approved` |
   | Order Canceled | `wix.ecom.v1.order` / `canceled` |
   | Payment Status Updated | `wix.ecom.v1.order` / `payment_status_updated` |
   | Order Transactions Refund Completed | `order_transactions` / `refund_completed` |
   | Order Transactions Details Updated | `order_transactions` / `details_updated` |

   Permission: **Read Orders** (`SCOPE.DC-STORES.READ-ORDERS`) covers every one
   of them.

2. **Get Public Key** on the same page (also on the app's home page under *View
   ID and Keys*). Set the PEM as the `WIX_APP_PUBLIC_KEY` environment variable
   on the API service. Real newlines are preferred; a single line with `\n`
   escapes is accepted, because that is how a PEM survives most secret UIs.
   Without it every delivery is answered **503** and Wix retries — no event is
   lost while the key is missing.

### In Pivota

The connect call must carry the app's **instance id** for that site:

```
POST /merchant-stores/wix/connect
{ "merchant_id": "...", "site_id": "...", "api_key": "...",
  "instance_id": "<the app instance id on this site>" }
```

`instance_id` is optional and new. Without it the credential is stored exactly
as before (a bare API key string) and catalog sync is unaffected — the store
simply receives no telemetry, because `POST /webhooks/wix` has nothing to
resolve it by. With it, the credential is stored as the JSON blob every Wix
reader in this repo already understands (`normalize_wix_api_key`,
`extract_wix_site_id`, `adapters/wix_adapter.py::_wix_credentials`).

### Limitation: API-key mode cannot receive webhooks

Pivota has two Wix connect modes, and only one of them can ever produce
telemetry:

* **API-key mode** (`POST /merchant-stores/wix/connect`) — the live path.
  The merchant pastes a Wix API key and a site id. **The Pivota app is not
  installed on the site**, so Wix has no app instance there, no `instanceId`
  exists, and no webhook is ever delivered. Catalog sync and order writeback
  work; telemetry does not. A merchant in this mode who wants telemetry must
  install the app and supply the resulting `instance_id`.
* **OAuth app mode** (`GET /merchant-stores/wix/oauth/start` and
  `/callback`) — still a **501 stub** at the time of writing. It reads
  `WIX_APP_CLIENT_ID` / `WIX_APP_CLIENT_SECRET` only to report whether they are
  configured, and persists nothing. When that stub is implemented it must
  persist the `instanceId` from the app-instance exchange into the same
  credential blob key this bridge reads (`instance_id`); the reader side is
  already in place.

---

## Store resolution

The delivery names its site only by `instanceId` — the REST payload carries no
site id and no shop domain — so that is the whole of store resolution, and it
is read from the **verified claim**, never from the raw body.

`instanceId` lives inside the credential JSON, which SQLite and Postgres cannot
be asked to index the same way, so the lookup uses a `LIKE` to **narrow the
scan** over active `platform='wix'` stores and then compares exactly in Python.
A substring match can never resolve a store. The instance id is also shape-
checked before it reaches the `LIKE`, which keeps `%`/`_` out of the pattern.

An unknown or inactive instance answers **404**, like the static Shopify
receiver, so Wix stops retrying a delivery for a site we do not have.

## Auth chain

1. 1 MB body cap → 413
2. `WIX_APP_PUBLIC_KEY` configured → else 503
3. JWT verified: RS256 only, signature, and `exp` when present → else 401
4. `instanceId` read from the verified claim → else 401
5. store resolved by exact `instanceId` on an active Wix store → else 404
6. `identify(merchant_id, store_id)` + `enforce_rate_limit("platform", store_id)`
7. unsupported event → 200 `ignored` (before any Wix API call)
8. order read back for the two transactions events → 503 on failure
9. map → `ingest_merchant_event_batch(write_path="wix_webhook",
   agent_identity_confidence="platform_asserted")` → authority `platform`

## Event mapping

| Wix event | Order/transaction state | Canonical events |
|---|---|---|
| any `order` event | always | `order.created` @ `createdDate`, amount `priceSummary.total.amount` |
| any `order` event | `paymentStatus` ∈ {`PAID`, `PARTIALLY_REFUNDED`, `FULLY_REFUNDED`} | + `order.paid` @ `updatedDate` |
| any `order` event | `status` = `CANCELED` | + `order.cancelled` @ `updatedDate` |
| any `order` event | `paymentStatus` = `DECLINED` | + `payment.failed` @ `updatedDate` |
| `order_transactions` | `payments[].status` = `DECLINED` | `payment.failed` per payment id |
| `order_transactions` | each refund with a settled amount | `refund.succeeded` per **refund id** @ its own `createdDate` |

`order.created` is emitted on *every* order delivery, keyed on the order id, so
an order whose `created` webhook was missed still enters the ledger on its next
update and a repeat is a duplicate rather than a second order.

`PARTIALLY_PAID` deliberately does **not** emit `order.paid`: part of the
balance is still owed, so the order total would overstate what was captured.
`PARTIALLY_REFUNDED` / `FULLY_REFUNDED` do, because a refund presupposes a
capture — and the refund magnitude rides on its own events, so nothing is
double-counted.

A refund's magnitude is `summary.refunded` ("the portion of `requestedRefund`
that refunded successfully"), falling back to the sum of its `SUCCEEDED`
transactions. `requestedRefund` and `PENDING`/`FAILED` transactions are requests,
not money movement, and are never read as an amount; a refund with nothing
settled is answered 200 `ignored` and lands when its `refund_completed`
delivery arrives.

Refund events are keyed on the **native refund id**, so two partial refunds of
one order are two ledger facts that dedupe independently across the repeated
deliveries that re-offer the whole `orderTransactions.refunds` list.

## `order_ref`

`wix:<order GUID>` normally; **`pivota:<pivota order id>`** for an order Pivota
wrote back.

The Wix writeback (`adapters/wix_adapter.py::build_wix_order_payload`) stamps
the Pivota order id in two places, and only one is safe to read:

* `buyerNote` — documented as the "Buyer note left by the customer". It is buyer
  free text, so reading it would let a shopper type
  `Pivota Order ID: <someone else's>` at checkout and merge their order into
  another interaction. **Never read.** (Same hazard as BigCommerce's
  `customer_message`, which is why that bridge has no `pivota:` path at all.)
* `channelInfo.externalOrderId` — "Reference to an order ID from an external
  system", set by whoever *records* the order through the API, alongside
  `channelInfo.type = OTHER_PLATFORM`. A buyer checking out on the storefront
  gets `channelInfo.type = WEB` and no `externalOrderId`; the field appears on
  no buyer-facing form. **This is the structured, non-buyer-writable marker**,
  and it is what the mapper reads — the same contract as Shopify's
  `pivota_order_id` note attribute and WooCommerce's order meta.

Residual, accepted: another app on the *same site* could also record an external
order with `OTHER_PLATFORM` and an id from its own system. That is a
merchant-scoped confusion inside one store, not a cross-merchant one, and it is
the same exposure the Shopify and WooCommerce bridges already carry.

## What is deliberately not collected

* **`click_id` is `None`.** No Wix field carries a Pivota click id: the
  writeback stamps none, and nothing on a storefront order (`channelInfo`,
  `customFields`, `extendedFields` — the last needs a Dev Center schema we do
  not have) holds one today.
* **No PII.** `buyerInfo.email`, `billingInfo.contactDetails` (names, phone),
  every address, and `refund.details.reason` (customer free text) are never
  copied into metadata. `buyer_id` is `buyerInfo.contactId` (or `memberId`),
  which are opaque GUIDs.
* **The metadata allowlist was not widened.** Everything this bridge records
  fits existing keys.
* **No merchant-facing webhook status endpoint.** The honest question is "is the
  Pivota app installed on this site?", and there is no cheap way to answer it:
  the only signal we hold is the `instance_id` the merchant typed in themselves,
  so the endpoint would echo back its own input plus a global env boolean. Not
  worth an authenticated route.

[about]: https://dev.wix.com/docs/build-apps/develop-your-app/api-integrations/events-and-webhooks/about-webhooks.md
[no-sdk]: https://dev.wix.com/docs/build-apps/develop-your-app/develop-a-self-managed-app/webhooks/handle-events-with-webhooks-for-self-hosting-without-the-java-script-sdk.md
[structure]: https://dev.wix.com/docs/api-reference/articles/work-with-wix-apis/platform/about-the-structure-of-webhooks.md

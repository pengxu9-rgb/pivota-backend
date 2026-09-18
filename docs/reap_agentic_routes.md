# `/agent/v2/commerce/reap` — the contract, for the gateway (WP5)

Three routes. `routes/agent_commerce_reap.py` is the implementation and
`docs/runbooks/reap_agentic_purchase.md` is the operational half; this page is the wire.

**The rail is DARK.** `REAP_AGENTIC_ENABLED` is unset in production, so every route below answers
**404 `not_available_on_this_rail`** today. That is the state to build the fallback against: the
door should treat a 404 from this prefix as "this rail is not available for this purchase" and go
somewhere else, exactly as it would for a merchant that is not eligible.

---

## Authentication

Both headers, on every route.

| header | authenticates | required |
|---|---|---|
| `X-API-Key` | the calling **agent** | yes — `routes/agent_auth.get_agent_context` |
| `X-Agent-User-JWT` | the **end user** (the buyer) | **yes** — a purchase needs a buyer |

`get_agent_context` is the repo's shared agent dependency, and an API key is not its only input:
it also accepts the **checkout-token** path used elsewhere in the agent surface, so a caller
holding a valid checkout token resolves to an agent context without presenting `X-API-Key`.
Nothing on this rail depends on which of the two it was — the ownership conjunct is on
`context.agent_id` either way — but a client that assumes "no API key means 401" will be wrong.

A purchase is opened for the buyer that `buyer_identity_links` maps
`(agent_id, hash(agent_user_ref))` to. **There is no field in any request body that names a
buyer**, and there is no way for an agent to act for a buyer it has not been given a user token
for.

Missing or empty `X-Agent-User-JWT` → **401 `agent_user_required`**. (401 and not 403: 403 would
assert that we know who the buyer is and are refusing them. We do not know — there is no token.
A token that is *present and invalid* already answers 401 from `routes/agent_user_auth`.)

---

## The error envelope — read this before you write a client

The app wraps **every** 4xx/5xx JSON response in a house shape
(`middleware/error_handler.ErrorHandlerMiddleware`). These routes write `{"error": "<reason>"}`
and the middleware preserves it verbatim under `detail`:

```json
{
  "status": "error",
  "error": {
    "code": "CONFLICT",
    "message": "merchant_not_eligible",
    "details": {
      "error": "merchant_not_eligible"
    },
    "documentation_url": "https://api.pivota.cc/agent/docs/overview"
  },
  "metadata": {
    "timestamp": "2026-09-18T07:16:01.989518Z",
    "request_id": "6187cbd5-5876-4962-bdc1-b17e7dbbf1df"
  },
  "detail": {
    "error": "merchant_not_eligible"
  }
}
```

> Every JSON block on this page was **captured from a real response** of the real app against
> Postgres, not written by hand. Key names, key order, status codes and timestamp formats are what
> the wire emits. Note in particular that the envelope's `metadata.timestamp` ends in `Z` while
> every timestamp in a success body is an ISO offset (`+00:00`) — they are produced by different
> code and a client must parse both.

**Read `detail.error`.** It is the reason code this page documents, and it is the field the
route wrote. `error.code` is the middleware's generic status→code mapping and is *not* specific to
this rail — a 404 from here reports `PRODUCT_NOT_FOUND`, which means nothing about a product.

**This app cannot return 422.** The middleware rewrites every 422 to **400** and replaces the
details with a `validation_errors` list. So the "you can fix this by editing the request" class
answers **400**, not 422. Do not treat 400 as a transport error.

---

## `POST /agent/v2/commerce/reap/purchases`

Opens a purchase and returns **immediately**. **No partner call is made in this request** — the
poller drives the state machine afterwards, on another process, over the next minutes.

### Request

```json
{
  "merchant_domain": "brand.example",
  "product_key": "prod::m_brand::shopify::1001",
  "variant_key": "sku::prod::m_brand::shopify::1001::v1",
  "quantity": 1,
  "buyer": {
    "email": "ada@example.test",
    "name": "Ada Lovelace",
    "phone": "+15550100",
    "consent_version": "reap-agentic-v1",
    "shipping_address": {
      "firstName": "Ada",
      "lastName": "Lovelace",
      "phone": "+15550100",
      "addressLine1": "900 Brannan St",
      "addressLine2": "Suite 400",
      "city": "San Francisco",
      "region": "CA",
      "postalCode": "94103",
      "country": "US"
    }
  },
  "return_url": "https://agent.pivota.cc/reap/return",
  "idempotency_key": "door-7f3a-2026-09-18",
  "click_context": { "surface": "chat" }
}
```

| field | required | notes |
|---|---|---|
| `merchant_domain` | yes | lowercased. Must be **enabled** in `reap_agentic_eligibility` for the buyer's market. |
| `product_key` | yes | our catalog key (`catalog_products.product_key`). |
| `variant_key` | no | our sku key (`catalog_skus.sku_key`), matched **exactly**. Omit only when the product has exactly one variant; a multi-variant product with no `variant_key` is `row_not_found`. |
| `quantity` | no (default 1) | 1..10 (`MAX_QUANTITY`). |
| `buyer.email` | yes | the contact for **this purchase**. It is **not** an identity: nothing about the buyer we resolve or create is derived from it. See "The buyer identity" below. |
| `buyer.consent_version` | **yes** | the version tag of the terms your user accepted, ≤ 32 printable characters. Missing, blank, over-long or unprintable ⇒ `400 consent_required`. Stored against the buyer and **overwritten on every purchase**, so it is always the latest version they accepted. Not an enum — we record the tag, we do not adjudicate it — and not part of the idempotency hash, so re-consenting mid-retry still replays. |
| `buyer.shipping_address` | yes | **the Reap client's field names**, not the snake_case shape `/agent/v2/commerce/checkouts` uses. Required: `firstName`, `lastName`, `phone`, `addressLine1`, `city`, `country`. Optional: `addressLine2`, `region`, `postalCode`. Unknown keys are dropped. |
| `buyer.name`, `buyer.phone` | no | **fallbacks only.** Used when the address omits the field; never override it. `name` splits on the last space. |
| `return_url` | no | defaults to `REAP_AGENTIC_RETURN_URL`, else `https://agent.pivota.cc/reap/return`. Must be https, no userinfo, on a host in `REAP_RETURN_URL_HOSTS`. |
| `idempotency_key` | no | honoured for **24 hours**, scoped to `(agent, buyer)`. |
| `click_context` | no | accepted and not forwarded. The click id this rail records is one **we** mint. |

**There is no price field, and a price in the body is ignored.** The unit price comes from our
catalog and is the number `verify_quote` later compares against Reap's subtotal, exactly, with no
tolerance.

### Response — `202 Accepted`

```json
{
  "purchase_id": "rp_283fba3ce85c4e59bb331e54",
  "status": "resolving",
  "poll_after_seconds": 60
}
```

A **replay** (same `idempotency_key`, same agent, same buyer, inside 24 h, **and the same
request**) returns the same `purchase_id` and the purchase's **current** state, which may not be
`resolving`.

The key is compared together with a hash of the request it was used for: merchant, product,
variant, quantity, buyer email, shipping address and return url. Reuse a key on a **different**
body and the answer is `409 idempotency_conflict`, not a 202 naming a purchase of something else.
Values are compared after normalisation, so a retry that differs only in the casing of a domain,
or that supplies the recipient through `buyer.name` rather than in the address, still replays.

### Refusals

| status | `detail.error` | meaning | what the door should do |
|---|---|---|---|
| 404 | `not_available_on_this_rail` | the dial is off, or the Reap client is unconfigured | fall back |
| 401 | `agent_user_required` | no `X-Agent-User-JWT` | get a user token, or fall back |
| 409 | `merchant_not_eligible` | no enabled eligibility row for this domain **in the buyer's market** | fall back |
| 409 | `buyer_unlinked` | **you should never see this.** Since WP4b the buyer identity is created on the first purchase, so this no longer means "no link" — it is the fail-closed answer when the identity or the opaque ref could not be *stored* (a storage fault, not a request fault). Retrying is reasonable; editing the body will not help. | retry once, then fall back |
| 409 | `row_not_found` | no such product under this domain, or the variant is not this product's, or no variant named and the product has more than one | fall back |
| 409 | `row_not_shopify` | the catalog row's intake lane is not `shopify` | fall back |
| 409 | `row_unpriced` | **this merchant** has no usable offer of its own on the sku, or the price is not exactly representable in minor units | fall back |
| 409 | `row_currency_mismatch` | the offer is priced in a currency the buyer's market does not use | fall back |
| 409 | `idempotency_conflict` | this key was already used for a **different** request | use a new key, or re-send the original request |
| 400 | `consent_required` | `buyer.consent_version` is missing, blank, longer than 32 characters, or carries an unprintable character | show your user the terms, then resend with the tag |
| 400 | `invalid_request` | the body is not a JSON object, did not validate, `quantity` out of range, `limit` out of range, or an identifier carries an unprintable character | fix the request |
| 400 | `invalid_address` | the shipping address is incomplete or unprintable | fix the request |
| 400 | `invalid_return_url` | not https, carries userinfo, or an unallowed host | fix the request |
| 400 | `currency_unsupported` | a three-decimal currency; this rail's converter assumes two | fall back |

No refusal ever carries the buyer's email or address, and none carries Reap's text.

**`row_unpriced` is merchant-scoped.** `catalog_offers.merchant_id` is the offer **seller**, and a
sku can carry offers from several. This route reads only the eligible merchant's own offer — a
cheaper offer from somebody else is not a price we are entitled to commit a buyer's card to, and
using it would produce a `price_changed` refusal at the quote for a change that never happened.

**`row_currency_mismatch` is the domestic rule applied to money.** The market is `US` → the offer
must be priced in `USD`. A market the rail does not have a currency for refuses too: this fails
closed, and adding a market is a one-line change (`_MARKET_CURRENCY` in
`routes/agent_commerce_reap.py`), documented in the runbook.

#### Real refusal bodies

```json
{
  "status": "error",
  "error": {
    "code": "CONFLICT",
    "message": "idempotency_conflict",
    "details": {
      "error": "idempotency_conflict"
    },
    "documentation_url": "https://api.pivota.cc/agent/docs/overview"
  },
  "metadata": {
    "timestamp": "2026-09-18T07:16:01.994097Z",
    "request_id": "e2efa9e0-ac14-4ea0-86af-9d06344645c1"
  },
  "detail": {
    "error": "idempotency_conflict"
  }
}
```

The dark rail, which is what every route answers today — and answers for **every** shape of input,
including a body that is not JSON, the wrong content-type, and `?limit=500`:

```json
{
  "status": "error",
  "error": {
    "code": "PRODUCT_NOT_FOUND",
    "message": "not_available_on_this_rail",
    "details": {
      "error": "not_available_on_this_rail"
    },
    "documentation_url": "https://api.pivota.cc/agent/docs/overview"
  },
  "metadata": {
    "timestamp": "2026-09-18T07:16:01.997697Z",
    "request_id": "6a6a4068-2286-443b-8db4-78978d24e9ee"
  },
  "detail": {
    "error": "not_available_on_this_rail"
  }
}
```

> **`/openapi.json` still lists these three paths while the rail is dark.** The routes are mounted
> at import and the schema is built from the router, so the *schema* is not gated even though every
> *response* is. That residue is known and deliberate — a router mounted only when a dial is on is
> a router whose mounting is never exercised — and it is the only way to tell from outside that the
> rail exists at all.

---

## `GET /agent/v2/commerce/reap/purchases/{purchase_id}`

Scoped to `(agent_id, agent_user_ref_hash)` **in SQL**. A purchase belonging to another agent, or
to another end user of the same agent, answers **404 `purchase_not_found`** — the same answer as
one that does not exist, so this endpoint cannot be used to probe for ids.

`needs_enrollment` — the buyer has a card page to open, and nothing has been quoted yet:

```json
{
  "id": "rp_283fba3ce85c4e59bb331e54",
  "state": "needs_enrollment",
  "merchant_domain": "brand.example",
  "product_key": "prod::m_brand::shopify::1001",
  "variant_key": "sku::prod::m_brand::shopify::1001::v1",
  "product_name": "Standard Eau de Parfum",
  "variant_title": "Standard",
  "brand": "Brand",
  "category": "fragrance",
  "quantity": 1,
  "reap_quote_expires_at": null,
  "refusal_reason": null,
  "last_error_code": null,
  "created_at": "2026-09-18T07:15:49.926588+00:00",
  "updated_at": "2026-09-18T07:15:49.959009+00:00",
  "terminal_at": null,
  "totals": {
    "currency": "USD",
    "our_price_minor": 4250,
    "quoted_total_minor": null,
    "final_total_minor": null,
    "shipping_minor": null,
    "tax_minor": null
  },
  "hosted_url": "https://pay.prava.space/enroll/3fa85f64",
  "hosted_url_expires_at": "2026-09-18T08:15:49.957734+00:00",
  "poll_after_seconds": 30
}
```

`awaiting_approval` — quoted, and the buyer has an approval page:

```json
{
  "id": "rp_283fba3ce85c4e59bb331e54",
  "state": "awaiting_approval",
  "merchant_domain": "brand.example",
  "product_key": "prod::m_brand::shopify::1001",
  "variant_key": "sku::prod::m_brand::shopify::1001::v1",
  "product_name": "Standard Eau de Parfum",
  "variant_title": "Standard",
  "brand": "Brand",
  "category": "fragrance",
  "quantity": 1,
  "reap_quote_expires_at": "2026-09-18T07:20:49.964987+00:00",
  "refusal_reason": null,
  "last_error_code": null,
  "created_at": "2026-09-18T07:15:49.926588+00:00",
  "updated_at": "2026-09-18T07:15:49.965465+00:00",
  "terminal_at": null,
  "totals": {
    "currency": "USD",
    "our_price_minor": 4250,
    "quoted_total_minor": 4500,
    "final_total_minor": null,
    "shipping_minor": 100,
    "tax_minor": 150
  },
  "hosted_url": "https://pay.prava.space/checkout/chk_7f3a",
  "hosted_url_expires_at": "2026-09-18T08:15:49.964985+00:00",
  "poll_after_seconds": 30
}
```

`completed` — note that `hosted_url` and `hosted_url_expires_at` are **gone**, not null, and
`order_reference` has appeared:

```json
{
  "id": "rp_283fba3ce85c4e59bb331e54",
  "state": "completed",
  "merchant_domain": "brand.example",
  "product_key": "prod::m_brand::shopify::1001",
  "variant_key": "sku::prod::m_brand::shopify::1001::v1",
  "product_name": "Standard Eau de Parfum",
  "variant_title": "Standard",
  "brand": "Brand",
  "category": "fragrance",
  "quantity": 1,
  "reap_quote_expires_at": "2026-09-18T07:20:49.964987+00:00",
  "refusal_reason": null,
  "last_error_code": null,
  "created_at": "2026-09-18T07:15:49.926588+00:00",
  "updated_at": "2026-09-18T07:15:49.970321+00:00",
  "terminal_at": "2026-09-18T07:15:49.970321+00:00",
  "totals": {
    "currency": "USD",
    "our_price_minor": 4250,
    "quoted_total_minor": 4500,
    "final_total_minor": 4500,
    "shipping_minor": 100,
    "tax_minor": 150
  },
  "order_reference": "ord_991",
  "poll_after_seconds": null
}
```

### Field rules the door must not guess at

* **Timestamps are ISO-8601 with an explicit offset — `2026-09-18T07:15:49.926588+00:00`, not
  `...Z`.** Microseconds are present. (The *error envelope's* `metadata.timestamp` does end in `Z`;
  they come from different code.)
* **Absent and null are different, and both occur.** `hosted_url` / `hosted_url_expires_at` are
  **absent** whenever there is nowhere to send the buyer; `reap_quote_expires_at` is **present and
  null** before anything has been quoted, and so are the unfilled members of `totals`. Read with
  `.get()`, and do not treat a missing key as an error.
* **`hosted_url` / `hosted_url_expires_at` appear only when** `state` is `needs_enrollment` or
  `awaiting_approval`, the URL still passes the host allowlist, and it has not expired. Never
  cache one past `hosted_url_expires_at`.
* **`order_reference` appears only on `completed`.**
* **`poll_after_seconds`** is the rail's interval for the current state, and `null` on a terminal
  state (`completed`, `failed`, `refused`, `expired`).
* **`refusal_reason`** (on `refused`) is our vocabulary, sometimes carrying the resolver's own
  reason verbatim (e.g. `options:sole_label_differs:size`). Diagnostic, not an enum to branch on.
* **What is never here:** the buyer's email or address; `buyer_ref`, `agent_id`,
  `agent_user_ref_hash`; `reap_product_id`, `reap_variant_id`, `reap_quote_id`,
  `reap_checkout_id`; `enrollment_id`, `click_id`, `return_url`; any Reap media or image URL.
  The body is built from `db/reap_agentic_ledger.PUBLIC_PURCHASE_COLUMNS`, an allowlist.

### States the door will see

`resolving` → `needs_enrollment` (buyer must enrol a card) → `quoting` → `awaiting_approval`
(buyer must approve) → `processing` → `completed`. Also `refused`, `failed`, `expired`.
`resolving` may go straight to `quoting` when the buyer is already enrolled. The buyer needs a
link in exactly the two states named above.

---

## `GET /agent/v2/commerce/reap/purchases?limit=20`

Same ownership scope. Newest first. `limit` is 1..100, default 20; anything else — out of range or
not a number — is **400 `invalid_request`**, not a silent clamp, because a caller that asked for
500 and got 20 has been handed a page it does not know is partial.

```json
{
  "purchases": [
    {
      "id": "rp_283fba3ce85c4e59bb331e54",
      "state": "completed",
      "merchant_domain": "brand.example",
      "product_key": "prod::m_brand::shopify::1001",
      "variant_key": "sku::prod::m_brand::shopify::1001::v1",
      "product_name": "Standard Eau de Parfum",
      "variant_title": "Standard",
      "brand": "Brand",
      "category": "fragrance",
      "quantity": 1,
      "reap_quote_expires_at": "2026-09-18T07:20:49.964987+00:00",
      "refusal_reason": null,
      "last_error_code": null,
      "created_at": "2026-09-18T07:15:49.926588+00:00",
      "updated_at": "2026-09-18T07:15:49.970321+00:00",
      "terminal_at": "2026-09-18T07:15:49.970321+00:00",
      "totals": {
        "currency": "USD",
        "our_price_minor": 4250,
        "quoted_total_minor": 4500,
        "final_total_minor": 4500,
        "shipping_minor": 100,
        "tax_minor": 150
      },
      "order_reference": "ord_991",
      "poll_after_seconds": null
    }
  ],
  "limit": 20
}
```

Each element has exactly the same shape as the single read.

---

## The buyer identity — read this before planning the integration

**Linking is automatic on the first purchase.** An agent-only buyer — one we know only through an
agent user token — gets a buyer identity created for them by their first `POST /purchases`, and
every purchase after that resolves to the same one. There is nothing to call first and no
enrollment step to build.

This reverses what this page said through WP4, when `buyer_unlinked` was the answer for every
agent-only buyer. Owner decision, 2026-09-18.

### What gets created, and what does not

On the first purchase, when `(agent_id, hash(agent_user_ref))` has no row:

| created | not created |
|---|---|
| a `buyer_identity_links` row binding that pair to a new buyer id | a `shop_users` account |
| an opaque `reap_agentic_buyer_refs` row — the `owner.id` Reap enrols the card against | anything derived from `buyer.email` |

The buyer id is **random**. It is not derived from the email, the user ref, or anything else in
the request, and no account row is created for it. That is a security property, not an
implementation detail: the repo's account writer is *create-or-get* — handed an email that already
has an account it returns **that account**. Minting through it would let an agent that asserted a
stranger's email be handed the stranger's real buyer id, and with it their saved email and default
shipping address on the next checkout intent. The identity an agent asserts is unverified, so it
lives in its own space and can never collide with a verified one.

The cost of having no account row is a checkout-intent **prefill**, which returns nothing for
these buyers — exactly what it returned for them before, when there was no link at all. Nothing
regresses.

### An existing link always wins

If the buyer signed in through the hosted checkout, that link already exists and this route uses
it unchanged — it never repoints a link a human's sign-in established. Two concurrent first
purchases produce **one** buyer, **one** link and **one** ref: the insert cannot overwrite, and
the route re-reads rather than trusting what it minted.

### The one thing WP5 must plan for

If the same human **later** signs in through the hosted checkout, `POST /buyer/save_from_checkout`
repoints the link to their real account — correctly, a verified account supersedes a placeholder.
Because `reap_agentic_buyer_refs` is keyed on the buyer id, their next purchase mints a fresh ref
and **Reap asks for the card once more**. One re-enrollment after a sign-in, once.

That is the trade the owner took. WP4 avoided it by refusing every agent-only buyer forever, which
made the rail unusable for the door it exists for.

---

## `consent_required` — the gate in front of all of this

`buyer.consent_version` is **required** on every `POST /purchases`. Omit it and the answer is
`400 consent_required`, decided before eligibility, before the catalog is read, and before
anything is written — a buyer who has not consented gets no identity, no purchase, and no
information about which merchants we have enabled.

It exists because WP4b took a human out of the loop. Before it, the buyer arrived already linked
by a surface where they had signed in, and **that sign-in was the consent**. Creating the identity
from an agent's assertion removes that step, so the door has to carry the act forward on the
request that uses it.

* **It is a version tag, not prose.** The wording is Pivota's and is rendered by the door; what
  the backend records is *which* wording was shown. ≤ 32 printable characters.
* **We do not adjudicate it.** There is no allowlist of known versions — a backend that refused
  an unrecognised tag would reject the newest consent the moment the door shipped it.
* **Latest wins.** It is rewritten on every purchase, alongside a `consented_at` timestamp.
* **It is not in the idempotency hash.** A retry that carries a newer tag still replays to the
  same purchase rather than answering `idempotency_conflict`.

The dial is still checked **first**: a dark rail answers `404` to a request with no consent, the
same as to every other request, so this field cannot be used to probe whether the rail is armed.

---

## Latency and polling

`POST` returns without a partner call, so it is fast. Everything after it is the poller's, on a
30-second cadence by default; one `quoting` step can take up to ~170 s. The door should poll
`GET` at `poll_after_seconds` and must not assume a hosted URL exists on the first read after
the POST — `resolving` has no page yet.

There are **no webhooks on this rail**. The poll is the only way an outcome is ever learned.

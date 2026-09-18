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
    "details": { "error": "merchant_not_eligible" },
    "documentation_url": "https://api.pivota.cc/agent/docs/overview"
  },
  "detail": { "error": "merchant_not_eligible" },
  "metadata": { "timestamp": "2026-09-18T06:24:25.034990Z", "request_id": "934722d1-…" }
}
```

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
| `buyer.email` | yes | |
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
{ "purchase_id": "rp_1bffbe0fca434e14bc42bf20", "status": "resolving", "poll_after_seconds": 60 }
```

A **replay** (same `idempotency_key`, same agent, same buyer, inside 24 h) returns the same
`purchase_id` and the purchase's **current** state, which may not be `resolving`.

### Refusals

| status | `detail.error` | meaning | what the door should do |
|---|---|---|---|
| 404 | `not_available_on_this_rail` | the dial is off, or the Reap client is unconfigured | fall back |
| 401 | `agent_user_required` | no `X-Agent-User-JWT` | get a user token, or fall back |
| 409 | `merchant_not_eligible` | no enabled eligibility row for this domain **in the buyer's market** | fall back |
| 409 | `buyer_unlinked` | no `buyer_identity_links` row for this agent user | fall back (this rail needs a durable buyer; see the runbook) |
| 409 | `row_not_found` | no such product under this domain, or the variant is not this product's, or no variant named and the product has more than one | fall back |
| 409 | `row_not_shopify` | the catalog row's intake lane is not `shopify` | fall back |
| 409 | `row_unpriced` | no usable offer, or a price we cannot state exactly in minor units | fall back |
| 400 | `invalid_request` | the body did not validate (includes `quantity` out of range) | fix the request |
| 400 | `invalid_address` | the shipping address is incomplete or unprintable | fix the request |
| 400 | `invalid_return_url` | not https, carries userinfo, or an unallowed host | fix the request |
| 400 | `currency_unsupported` | a three-decimal currency; this rail's converter assumes two | fall back |

No refusal ever carries the buyer's email or address, and none carries Reap's text.

---

## `GET /agent/v2/commerce/reap/purchases/{purchase_id}`

Scoped to `(agent_id, agent_user_ref_hash)` **in SQL**. A purchase belonging to another agent, or
to another end user of the same agent, answers **404 `purchase_not_found`** — the same answer as
one that does not exist, so this endpoint cannot be used to probe for ids.

```json
{
  "id": "rp_1bffbe0fca434e14bc42bf20",
  "state": "awaiting_approval",
  "merchant_domain": "brand.example",
  "product_key": "prod::m_brand::shopify::1001",
  "variant_key": "sku::prod::m_brand::shopify::1001::v1",
  "product_name": "Standard Eau de Parfum",
  "variant_title": "Standard",
  "brand": "Brand",
  "category": "fragrance",
  "quantity": 1,
  "totals": {
    "currency": "USD",
    "our_price_minor": 4250,
    "quoted_total_minor": 4500,
    "final_total_minor": null,
    "shipping_minor": 100,
    "tax_minor": 150
  },
  "hosted_url": "https://pay.prava.space/checkout/chk_7f3a",
  "hosted_url_expires_at": "2026-09-18T21:00:00Z",
  "reap_quote_expires_at": "2026-09-18T18:05:00Z",
  "refusal_reason": null,
  "last_error_code": null,
  "created_at": "2026-09-18T17:55:02Z",
  "updated_at": "2026-09-18T18:00:11Z",
  "terminal_at": null,
  "poll_after_seconds": 30
}
```

Completed:

```json
{
  "id": "rp_1bffbe0fca434e14bc42bf20",
  "state": "completed",
  "order_reference": "ord_991",
  "totals": { "currency": "USD", "our_price_minor": 4250, "quoted_total_minor": 4500,
              "final_total_minor": 4500, "shipping_minor": 100, "tax_minor": 150 },
  "terminal_at": "2026-09-18T18:12:40Z",
  "poll_after_seconds": null
}
```

### Field rules the door must not guess at

* **`hosted_url` / `hosted_url_expires_at` are present only when there is somewhere to send the
  buyer**: `state` is `needs_enrollment` or `awaiting_approval`, the URL still passes the host
  allowlist, and it has not expired. Otherwise **both keys are absent** — not null, absent.
  Show the link when it is there; never cache it past `hosted_url_expires_at`.
* **`order_reference` appears only on `completed`.** On every other state the key is absent.
* **`poll_after_seconds`** is the rail's own interval for the current state, and `null` on a
  terminal state. Terminal states are `completed`, `failed`, `refused`, `expired`.
* **`refusal_reason`** (on `refused`) is our vocabulary, sometimes carrying the resolver's own
  reason verbatim (e.g. `options:sole_label_differs:size`). It is diagnostic, not an enum to
  branch on.
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

Same ownership scope. Newest first.

```json
{ "purchases": [ { "id": "rp_…", "state": "awaiting_approval", "totals": { }, "…": "…" } ],
  "limit": 20 }
```

`limit` is 1..100, default 20. Over 100 → **400** (FastAPI's own refusal, rewritten from 422 by
the envelope middleware). Each element has exactly the same shape as the single read.

---

## Latency and polling

`POST` returns without a partner call, so it is fast. Everything after it is the poller's, on a
30-second cadence by default; one `quoting` step can take up to ~170 s. The door should poll
`GET` at `poll_after_seconds` and must not assume a hosted URL exists on the first read after
the POST — `resolving` has no page yet.

There are **no webhooks on this rail**. The poll is the only way an outcome is ever learned.

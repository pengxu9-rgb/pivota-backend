# `/agent/v2/commerce/reap` — the contract, for the gateway (WP5)

`routes/agent_commerce_reap.py` implements this contract.
`docs/runbooks/reap_agentic_purchase.md` is the operational half; this page is the wire.

**New purchases are disabled by default.** The base gate or missing credentials make
create and list return **404 `not_available_on_this_rail`**; the create-only gate blocks
new purchases independently. Owner-scoped purchase GET and exact original-attempt recovery
remain available while create is paused. After the buyer selects Reap, no refusal,
unavailable response or uncertain outcome authorizes another checkout route or a cart-link
retry. Preserve the original body, key and buyer session for read-only recovery.

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

## `POST /agent/v2/commerce/reap/purchases/prepare`

Resolves a buyer-selected numeric Shopify variant to its **existing catalog SKU key** before
creating the original purchase body or idempotency key. Both agent and buyer authentication
apply. Master, credentials, create and cart-link gates must be live; a paused create refuses
preparation. The request has exactly `item_source: "cart_link"`, `merchant_domain`,
`product_key`, `variant_id`, `quantity` and `market_country`. Selector IDs are positive decimal
strings; quantity is a strict integer. Extra keys, caller price, buyer contact, consent,
idempotency and variant-key assertions are refused with `invalid_request`.

Success is HTTP 200 with a `selection` object containing `product_key`, `variant_id`,
`variant_key`, `merchant_domain`, `market`, `currency`, `unit_price_minor`, `quantity` and
`item_source`. `variant_key` is the stored SKU key, not a key constructed from `variant_id`.
The merchant field preserves the validated observed storefront host needed by the create
lane; pilot and merchant eligibility use the existing canonical merchant rule.

The selected numeric ID must resolve to exactly one unsuppressed, nonplaceholder SKU of that
product and seller/source. Duplicate aliases refuse `row_variant_ambiguous`; no SKU is chosen
by ordering or a default variant. The existing cart-link reader validates active attached
source, seller identity, current storefront proof and the selected SKU's own offer. Missing,
expired or contradictory evidence refuses. Preparation requires its usable own offers to
agree on exact currency and minor price, and enforces the same current agent, domain, market,
product, quantity, variant, currency and item-total pilot bound as create. The later provider
quote must still satisfy the authoritative full total cap, including shipping, tax and fees.

This endpoint performs only catalog/eligibility/proof reads: no buyer link, consent, key,
click, enrollment or purchase writes, and no provider, merchant, crawl or enrichment network
request. PostgreSQL enforces a read-only repeatable-read transaction. A witness is a current
selection description, **not a reservation or permission to skip create's checks**. The door
checks the selected witness before its first create dispatch, then saves the original body,
key and buyer session. A missing, refused or uncertain preparation never authorizes another
checkout route. Original-attempt recovery does not prepare again and remains independent of
current catalog/proof/scope while create is paused.

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
  "return_url": "https://api.pivota.cc/reap/return",
  "idempotency_key": "door-7f3a-2026-09-18",
  "expected_unit_price_minor": 4250,
  "expected_currency": "USD",
  "click_context": { "surface": "chat" }
}
```

| field | required | notes |
|---|---|---|
| `item_source` | no (default `reap_variant`) | Set to `cart_link` for Tier B. Choose the source explicitly before the first POST; never retry a refused variant purchase as cart-link. The cart-link lane remains unavailable (404) until `REAP_AGENTIC_CART_LINK_ENABLED` is on (the base and create gates, bounded pilot scope and fresh eligibility/proof checks must also pass; the quote uses Reap's published `externalCheckout` body since 2026-09-28). |
| `offer_code` | no | the buyer's own offer (coupon) code, either lane: a string of 1..128 characters with at least one non-whitespace character and no control character, sent to Reap **exactly as given** (not trimmed, not upper-cased). Empty, whitespace-only, over-long or control characters ⇒ `400 invalid_offer_code`; a non-string ⇒ `400 invalid_request`. Part of the idempotency hash when present (a retry that adds or changes a code is a different purchase). If Reap refuses the code (`OFFER_CODE_INVALID` / `OFFER_CODE_EXPIRED`) the purchase is re-quoted **once without it** and `offer_code_outcome` says so — tell your user before they approve. |
| `merchant_domain` | yes | a bare host name, sent as observed (`www.brand.example` or `brand.example`); anything else — a scheme, port, path, userinfo, IP or single label — is `400 invalid_request`. Matched **canonically**: lower case, one leading `www.` removed, so `www.brand.example` and `brand.example` are the same merchant (`wwwbrand.example` is not). Variant lane: must be enabled in `reap_agentic_eligibility`. Cart-link lane: must have a fresh `tierb_cart_link_eligibility` verdict, and builds its cart URL on the host as sent. Both are checked in the buyer's market. |
| `product_key` | yes | our catalog key (`catalog_products.product_key`). |
| `variant_key` | no | our sku key (`catalog_skus.sku_key`), matched **exactly**. Omit only when the product has exactly one variant; a multi-variant product with no `variant_key` is `row_not_found`. |
| `quantity` | no (default 1) | 1..10 (`MAX_QUANTITY`). |
| `buyer.email` | yes | the contact for **this purchase**. It is **not** an identity: nothing about the buyer we resolve or create is derived from it. See "The buyer identity" below. |
| `buyer.consent_version` | **yes** | the version tag of the terms your user accepted, ≤ 32 printable characters. Missing, blank, over-long or unprintable ⇒ `400 consent_required`. Stored against the buyer and **overwritten on every purchase**, so it is always the latest version they accepted. Not an enum — we record the tag, we do not adjudicate it — and not part of the idempotency hash, so re-consenting mid-retry still replays. |
| `buyer.shipping_address` | yes | **the Reap client's field names**, not the snake_case shape `/agent/v2/commerce/checkouts` uses. Required: `firstName`, `lastName`, `phone`, `addressLine1`, `city`, `country`. Optional: `addressLine2`, `region`, `postalCode`. Unknown keys are dropped. |
| `buyer.name`, `buyer.phone` | no | **fallbacks only.** Used when the address omits the field; never override it. `name` splits on the last space. |
| `return_url` | no | defaults to `REAP_AGENTIC_RETURN_URL`, else `https://<first REAP_RETURN_URL_HOSTS host>/reap/return` — with nothing set, `https://api.pivota.cc/reap/return`, a static page this backend serves. Must be https, no userinfo, on a host in `REAP_RETURN_URL_HOSTS`. |
| `idempotency_key` | **yes** (create and recovery) | nonempty after trimming, ≤ 128 printable characters; missing, blank, over-long or unprintable ⇒ `400 invalid_request` before anything is read or written. One key is one attempt: it is what makes a retry of an unknown outcome (a lost `202`, a `503 checkout_outcome_unknown`) the **same** purchase, and recovery can only find a keyed attempt. Immutable, scoped to `(agent, buyer)`, retained for the lifetime of the attempt. Same normalized body returns the original purchase regardless of age or terminal state; a different body is `409 idempotency_conflict`. An unverifiable legacy hash fails closed. Refusal tombstones also persist. A new intentional purchase requires a fresh key; age never permits rollover. |
| `expected_unit_price_minor`, `expected_currency` | **yes**, both | the unit money the buyer was shown, bound into the attempt. Partial or malformed ⇒ `400 invalid_request` before anything is read. **Both** omitted ⇒ `400 invalid_request` before any write, unless the key already names an attempt keyed without money (before 2026-10-04): that retry gets the read-only replay (see below). See "Immutable selected money on new attempts" below. |
| `click_context` | no | accepted and not forwarded. The click id this rail records is one **we** mint. |

**The expected pair is a check, never a price override.** The unit price comes from our
catalog and is the number `verify_quote` later compares against Reap's subtotal, exactly, with no
tolerance; an expected pair that differs from it is `409 price_changed`.

For `item_source: "cart_link"`, the same authenticated endpoint requires a **fresh ELIGIBLE Tier B
verdict** for `(merchant_domain, buyer.shipping_address.country)` before reading the catalog or
minting a buyer. It constructs the single-line Shopify permalink itself, including `country=` and
an owned `pivota_click_id`; the caller cannot provide a URL, variant ID, seller identity or price.
The catalog SKU must identify a numeric Shopify variant. For a mirrored external seed, a numeric
operator-entered `attached_variant_id` is **not** enough: the active same-market seed must be
attached to this catalog product and carry a dedicated, at-most-seven-day-old storefront proof
from one Shopify `.js` fetch (`scripts/backfill_shopify_variant_ids.py` is its only writer), of
one of two kinds:

* **sole variant** — the live storefront had exactly one variant, and it is the seed's; or
* **named variant** (`scope: "named_variant"`, 2026-09-29) — the product has several variants
  (shades, sizes), the seed **names exactly one** of them, and the live storefront lists that one,
  `available: true`. A seed names a variant by its single snapshot entry's stamped id and/or one
  numeric `variant=` on its own product URL on the shop host; the two must agree, and so must
  every numeric variant id the seed itself records (the entry's `variant_id` / `id`, and
  `selected_variant_id` / `default_variant_id`). A snapshot with two or more entries names none.
  The catalog's chosen sku must name **that same** variant — a row carrying only the synthetic
  `::canonical` placeholder is refused — and it is priced **only** from that variant's own sku
  offer, never from the product-level placeholder offer.

The proof's product URL must be the seed's own, over https on the shop's host. A contradictory
attached id, an unproven multi-variant product, or a synthetic canonical SKU without that evidence
is refused. The seller's own offer supplies the exact price and currency.

**Enrichment rows (option 2, dark).** A `catalog_enrichment_agent_v1` row (`product_key`
`ext:<slug>::<8hex>`, or `ext:retailer:<32hex>` for the retailer lane) is refused on this lane
while `REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED` is off (default), exactly as before the flag
existed (`row_variant_unverified`, or `row_not_found` when the posted host differs from the row's
`source_domain`). The flag arms nothing on its own: it is read only inside the cart-link lane and
also requires `REAP_AGENTIC_CART_LINK_ENABLED`. It is not an in-flight kill switch; the cart-link
dial remains that. With it on, such a row is bought only when all of these hold:

* **Store.** `merchant_domain` is the same storefront as the row's `canonical_url` host **and**
  its `source_domain`, after one `www.` fold on each side (`www.brand.example` = `brand.example`;
  a subdomain, suffix or lookalike is `row_not_found`). The cart URL is built on the host as sent.
* **Seller.** The row's `merchant_id` is exactly the observed seller id our own minting functions
  re-derive from it (`ext:retailer:` → the retailer's domain; otherwise brand + host), and a
  non-null `seller_ref` agrees. The offer's `agent_seed::…` owner and a seed's seller are never
  the seller. Else `seller_identity_unverified`.
* **Key.** The legacy collapsed key `ext:unknown::<8 hex>` (shared by many products) is
  `row_not_found`; the distinct `ext:unknown::<16 hex>` keys are ordinary keys.
* **Sku.** A `variant_key` must be one of the product's live skus (`row_not_found`). Without one,
  the product must be single-variant **in the catalog and on the storefront**, else
  **`row_variant_ambiguous`** (the lane never picks a variant nobody named):
  * the catalog may know at most ONE `::v:` sku, **suppressed ones counted** (a 3-shade line with
    two shades suppressed, or a two-size product with one size suppressed, is not single-variant);
  * that one sku is used if it is live, and its storefront proof must then be **sole-variant**
    (the handle has exactly one variant); a catalog holding one of a storefront's two sizes is
    refused;
  * a **folded shade** (its `sku_payload.source_handle` names another handle than the
    `canonical_url`'s, e.g. MAC `<parent>-nc10`) is one choice among a family and is never bought
    without a `variant_key`, even when it is the only shade the catalog holds; named, it is;
  * with no live real sku, the `<product_key>::canonical` placeholder is used, which the proof
    step accepts only when the product has **no** `::v:` sku at all, suppressed ones included,
    and the storefront handle has exactly one variant.
* **Proof.** A row in `enrichment_cart_variant_proofs` for exactly that (product, sku), written by
  the storefront proof job (the route creates the empty table on first use; if that CREATE fails,
  it refuses for 60 s without retrying the DDL), at most 72 hours old, `ok`, available, on the same store and handle,
  naming the sku's own Shopify id (`services/reap_enrichment_cart_proof.verify_enrichment_cart_proof`).
  The variant in the cart URL is the one this proof names and nothing else. Any refusal is
  `row_variant_unverified`.
* **Price.** The listing's own offers on that sku (the enrichment lane's, under its `agent_seed::`
  namespace, live, available, `source_ref` on the same store and the product's handle) must agree
  on one price in the buyer market's currency, and it must equal the price the proof read live.
  Otherwise `row_unpriced`, `row_price_ambiguous`, `row_currency_mismatch`, or **`row_price_stale`**
  (the catalog price moved since the proof). All of these are answered before a click or a
  purchase row exists.

`variant_title` on the 202 is whatever the sku's payload carries as `variant_title`; no live
enrichment row carries one today, so it is `null`.
A click row is recorded before the purchase opens so the later conversion has verified seller identity. The
cart-link quote checks shipping options and totals, but an ELIGIBLE merchant verdict alone does
not prove shipping for this buyer or every SKU.

### Response — `202 Accepted`

```json
{
  "purchase_id": "rp_283fba3ce85c4e59bb331e54",
  "status": "resolving",
  "poll_after_seconds": 60,
  "checkout_dispatch_state": "not_dispatched",
  "contact_reentry_required": false
}
```

`status`, `poll_after_seconds`, `checkout_dispatch_state` and `contact_reentry_required` are read
back from the **committed** purchase after the key is bound, not assumed, so a fresh create
normally answers `not_dispatched` / `false` but reports whatever a worker has already done. The two
last fields mean exactly what they mean on `GET` (see "Field rules" below).

**Cart-link lane only**, the body also carries **`variant_title`** (additive; no other field
changes, and the variant lane's body is exactly the five keys above):

```json
{
  "purchase_id": "rp_283fba3ce85c4e59bb331e54",
  "status": "resolving",
  "poll_after_seconds": 60,
  "checkout_dispatch_state": "not_dispatched",
  "contact_reentry_required": false,
  "variant_title": "07 BURGUNDY INK"
}
```

It is the live storefront's own title for the variant the permalink buys, recorded by the
storefront proof. On this lane **the buyer never picks the variant** — the seed names it — so show
it to the buyer before they approve ("Silky Matte Lip Ink — 07 BURGUNDY INK"). Display only: the
numeric variant in the cart URL is what is bought. `null` when the proof recorded no title
(proofs written before 2026-09-29). A single-variant product's title is often Shopify's literal
`Default Title`, returned as is. The same value is stored as the purchase's `variant_title`, so
`GET` returns it too.

A **replay** (same `idempotency_key`, same agent, same buyer, at any age, **and the same
request**) returns the same `purchase_id` and the purchase's **current** state, which may not be
`resolving` (and, on the cart-link lane, the same `variant_title`).

The key is compared together with a hash of the request it was used for: item source, merchant, product,
variant, quantity, buyer email, shipping address and return url. Reuse a key on a **different**
body and the answer is `409 idempotency_conflict`, not a 202 naming a purchase of something else.
Values are compared after normalisation, so a retry that differs only in the casing of a domain,
or that supplies the recipient through `buyer.name` rather than in the address, still replays.

### Refusals

| status | `detail.error` | meaning | what the door should do |
|---|---|---|---|
| 404 | `not_available_on_this_rail` | the dial is off, or the Reap client is unconfigured | show unavailable; preserve the selected route and original attempt |
| 401 | `agent_user_required` | no `X-Agent-User-JWT` | obtain the original buyer session; do not switch routes |
| 409 | `merchant_not_eligible` | no variant-lane row, or no fresh ELIGIBLE cart-link verdict, for this domain **in the buyer's market** | show blocked; do not retry through cart-link |
| 409 | `merchant_disabled` | an operator turned this merchant off: a variant-lane merchant row for this domain and market is disabled. Answered on **both** lanes | show blocked; do not try another Reap lane |
| 409 | `buyer_unlinked` | **you should never see this.** Since WP4b the buyer identity is created on the first purchase, so this no longer means "no link" — it is the fail-closed answer when the identity or the opaque ref could not be *stored* (a storage fault, not a request fault). Retrying is reasonable; editing the body will not help. | show unknown or unavailable; recover the exact original attempt before retrying |
| 409 | `row_not_found` | no such product under this domain, or the variant is not this product's, or no variant named and the product has more than one | show blocked; resolve the refusal before any new purchase intent |
| 409 | `row_not_shopify` | the catalog row's intake lane is not `shopify` | show blocked; resolve the refusal before any new purchase intent |
| 409 | `row_variant_unverified` | Tier B has no numeric Shopify variant verified from our catalog or active same-market seed | show blocked; resolve the refusal before any new purchase intent |
| 409 | `seller_identity_unverified` | the catalog seller identity does not agree with the offer owner | show blocked; resolve the refusal before any new purchase intent |
| 409 | `row_unpriced` | **this merchant** has no usable offer of its own on the sku, or the price is not exactly representable in minor units. On the **cart-link lane** "usable" includes "priced in the buyer market's currency", so a row whose offers are all in another currency answers `row_unpriced` here, not `row_currency_mismatch` | show blocked; resolve the refusal before any new purchase intent |
| 409 | `row_price_ambiguous` | cart-link lane, no `variant_key`: the catalog spells the ONE chosen Shopify variant with several skus, and this merchant's usable offers on them carry different prices | show blocked; resolve the refusal before any new purchase intent |
| 409 | `row_currency_mismatch` | the offer is priced in a currency the buyer's market does not use (variant lane, and the cart-link lane's enrichment rows; the cart-link lane otherwise reads only offers in the market's currency and answers `row_unpriced` instead) | show blocked; resolve the refusal before any new purchase intent |
| 409 | `row_price_stale` | cart-link lane, **enrichment rows only** (dark flag): the catalog offer's price differs from the price the storefront proof read live | show blocked; resolve the refusal before any new purchase intent |
| 409 | `row_variant_ambiguous` | cart-link lane, **enrichment rows only** (dark flag): no `variant_key`, and the product is not single-variant: two or more `::v:` skus in the catalog (suppressed ones counted), or a storefront handle with several variants | show blocked; resolve the refusal before any new purchase intent |
| 409 | `idempotency_conflict` | this key was already used for a **different** request | preserve the original key and body; recover its outcome before any new purchase intent |
| 400 | `consent_required` | `buyer.consent_version` is **absent, blank, longer than 32 characters, or carries an unprintable character** — i.e. a string-shaped value that is not usable | show your user the terms, then resend with the tag |
| 400 | `invalid_request` | `buyer.consent_version` is **present but not a string** (`123`, `true`, `{}`, `[]`, `1.5`) — a type error is a malformed body, not a missing act by a human, and the two codes tell you to do different things | fix the request |
| 400 | `invalid_request` | the body is not a JSON object, did not validate (including a missing or blank `idempotency_key`, a partial `expected_unit_price_minor`/`expected_currency` pair, or no pair on a key that names no earlier money-less attempt), `quantity` out of range, `limit` out of range, or an identifier carries an unprintable character | fix the request |
| 400 | `invalid_address` | the shipping address is incomplete or unprintable | fix the request |
| 400 | `invalid_return_url` | not https, carries userinfo, or an unallowed host | fix the request |
| 400 | `invalid_offer_code` | `offer_code` is empty, whitespace only, longer than 128 characters, or carries a control character | fix the request (or omit the code) |
| 400 | `currency_unsupported` | a three-decimal currency; this rail's converter assumes two | show blocked; resolve the refusal before any new purchase intent |
| 503 | `checkout_outcome_unknown` | storage could not establish the outcome: the purchase-and-key transaction failed, the read-back of the committed purchase failed, or the key's stored mapping is unreadable. The attempt **may** exist | show unknown; call `POST /purchases/recover` with the **same body and key**, never re-POST — and never under a new key. See "Recover a lost create response" |

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

> **`/openapi.json` still lists these paths while the rail is dark.** The routes are mounted
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
  "consent_version": "reap-agentic-v1",
  "consented_at": "2026-09-18T07:15:49.926588+00:00",
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
  "poll_after_seconds": 30,
  "checkout_dispatch_state": "not_dispatched",
  "contact_reentry_required": false
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
  "consent_version": "reap-agentic-v1",
  "consented_at": "2026-09-18T07:15:49.926588+00:00",
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
  "approval_deadline": "2026-09-18T07:20:49.964987+00:00",
  "poll_after_seconds": 30,
  "checkout_dispatch_state": "dispatched",
  "contact_reentry_required": false
}
```

**`approval_deadline` is the instant the buyer must approve by** — the EARLIER of
`reap_quote_expires_at` and `hosted_url_expires_at` (whichever is present; absent only when both
are null). It is NOT the page's own expiry. Measured 2026-09-25 in the Reap sandbox (two
checkouts, neither approved): the hosted page's `expiresAt` is created + 15 min, but the checkout
flips to **FAILED — not EXPIRED — 1–10 s after the quote's `expiresAt`** (created + 5 min) and
never passes PROCESSING. A door that read `hosted_url_expires_at` as the deadline showed the
buyer ten minutes of a link that no longer works. `hosted_url` is dropped once the approval
deadline has passed, not only once the page's expiry has; `approval_deadline` itself stays in the
body, past or not, until the row leaves `awaiting_approval` — usually the poller moving it to
`failed` with `last_error_code: "approval_window_lapsed"`, but also the expiry sweep (`expired`)
if the poller is dark, or `completed` when an approval landed inside the last poll interval.

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
  "consent_version": "reap-agentic-v1",
  "consented_at": "2026-09-18T07:15:49.926588+00:00",
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
  "poll_after_seconds": null,
  "checkout_dispatch_state": "dispatched",
  "contact_reentry_required": false
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
  `awaiting_approval`, the URL still passes the host allowlist, and the approval deadline has not
  passed. Never cache one past `approval_deadline` (on `awaiting_approval`) or
  `hosted_url_expires_at` (on `needs_enrollment`, where nothing is quoted yet).
* **`approval_deadline` appears only on `awaiting_approval`**, and only when at least one of the
  two expiries is known. Use it, not `hosted_url_expires_at`, as the buyer's deadline; fall back
  to `hosted_url_expires_at` only when it is absent.
* **`order_reference` appears only on `completed`.**
* **`poll_after_seconds`** is the rail's interval for the current state, and `null` on a terminal
  state (`completed`, `failed`, `refused`, `expired`).
* **`checkout_dispatch_state`** is always present on a purchase (GET, list, `/recover`,
  `/resume`, and the create `202` and its replays), except a retired attempt's `/recover` receipt,
  which is not a purchase (see Recover a lost create response). It says what the durable record proves about a Reap checkout
  for this purchase (`db/reap_continuation.dispatch_state`), never more:

  | value | meaning |
  |---|---|
  | `not_dispatched` | tracked purchase (opened since migration 256) with no checkout create in flight and no checkout or order stored |
  | `dispatch_started` | we committed to a checkout create and its outcome is not established — **a checkout may exist at Reap** |
  | `dispatched` | a Reap checkout or order is stored on the purchase |
  | `unknown` | a purchase opened before dispatch tracking: no evidence either way, which is **never** proof that nothing was sent |

  Only `not_dispatched` is negative evidence. Treat the other three as "a payment page may
  exist": keep polling, never open a replacement purchase.
* **`contact_reentry_required`** is always present and is `true` only when the purchase is in
  `resolving`, `needs_enrollment` or `quoting` **and** the buyer's contact (email, address,
  offer code) was erased by the contact-retention sweep. The sweep runs on the poller: a purchase
  nobody holds whose contact is older than `REAP_AGENTIC_CONTACT_MAX_AGE_SECONDS` (default 900,
  measured from creation or from the last accepted re-entry) loses it. Such a purchase makes no
  new quote or checkout until the buyer re-enters the same contact through
  `POST /purchases/{purchase_id}/resume` (below). It is `false` on every other state, including
  `awaiting_approval` / `processing`, whose contact is erased by the same sweep but which need
  nothing more from the buyer than the approval link. A contact-paused purchase that is not
  re-entered within the re-entry window (`REAP_AGENTIC_CONTACT_REENTRY_WINDOW_SECONDS`, default
  86400 = 24 h, settable 3600–604800, **measured from `contact_purged_at`**, the moment the sweep
  erased the contact) and carries no dispatch evidence of any kind is ended by the poller with
  `last_error_code: "contact_reentry_lapsed"`: `needs_enrollment` → `expired`,
  `resolving` → `failed`, `quoting` → `failed`. An accepted resume clears `contact_purged_at`, so
  a purchase paused again later starts a new window from its new erasure.
* **`refusal_reason`** (on `refused`) is our vocabulary, sometimes carrying the resolver's own
  reason verbatim (e.g. `options:sole_label_differs:size`). Diagnostic, not an enum to branch on.
* **`consent_version` / `consented_at`** (migration **233**) are the tag your door sent as
  `buyer.consent_version` on the `POST` that opened *this* purchase, and when. Never rewritten —
  a later purchase under a newer tag does not move them, and a terminal state does not clear
  them. `null` only on purchases opened before 233.
* **`offer_code` / `offer_code_outcome` / `totals.discount_minor` / `totals.tax_included`**
  (migration **247**). `offer_code` is what your door sent, as sent, while the purchase is in
  flight; it is buyer input, so a terminal state (`completed`, `failed`, `refused`, `expired`)
  clears it to `null` with the email and the address. `offer_code_outcome` is `null` until the
  quote, then one of: `applied` (Reap took `discount_minor` off), `no_discount` (Reap accepted the
  code and took nothing off), `dropped_invalid` / `dropped_expired` (Reap refused the code; the
  purchase is re-quoted without it — **the price the buyer approves has no discount**). A dropped
  code is never sent again on that purchase; if the step had no time left for the re-quote it is
  released and the next poll re-quotes without the code. `quoted_total_minor` is always Reap's own
  `finalAmount`, already net of any discount; we never compute one. `tax_included` is `true` when
  `tax_minor` is already inside the prices (tax-inclusive markets such as SG): do **not** add it to
  subtotal + shipping in that case.
* **What is never here:** the buyer's email or address; `buyer_ref`, `agent_id`,
  `agent_user_ref_hash`; `reap_product_id`, `reap_variant_id`, `reap_quote_id`,
  `reap_checkout_id`; `enrollment_id`, `click_id`, `return_url`; any Reap media or image URL.
  The body is built from `db/reap_agentic_ledger.PUBLIC_PURCHASE_COLUMNS`, an allowlist.

### Price witness: preflight quote, corroborated price change, live price (mig 258, dark)

Owner decision 2026-10-05. Three dials, **all default off**; with every one off nothing below
happens, no new key appears in any body, and the refusals are exactly the ones documented above --
with one exception: a purchase that reaches `quoting` still carrying a live price an earlier
witness recorded (a dial armed then, off now) has that price cleared, so the view never shows a
"price updated" that no current check stands behind. A purchase no dial ever touched carries none,
and nothing is written for it.

| dial | values (default) | what it does |
|---|---|---|
| `REAP_AGENTIC_PRICE_CORROBORATION` | `on`/`1`/`true`/`yes` arm it; anything else is off (off) | a quote whose items subtotal differs from `our_price_minor × quantity`, and that passes **every other** quote check, is compared with an **independent** live unit price of the purchase's own Shopify variant from our own storefront reads (below). Corroborated **lower** → the purchase continues at Reap's quote. Corroborated **higher** → terminal `refused` / `price_changed` with `last_error_code: "quote_price_increased_corroborated"`. Anything else → `price_changed` / `quote_items_subtotal_mismatch`, as today. |
| `REAP_AGENTIC_CORROBORATION_MAX_AGE_HOURS` | integer 1..168 (72) | how old an independent read may be. Out of range or not an integer → 72, warned once. |
| `REAP_AGENTIC_PREFLIGHT_MODE` | `off` / `shadow` / `enforce`, anything else is off (off) | in `resolving`, after the item resolves and **before any enrollment is created, reused or replayed**, one buy-intent `POST /agentic/quotes` (same body the approval quote sends, keyed apart from it; its quote id is never stored or checked out) is checked like the approval quote (with corroboration when that dial is on). `shadow`: recorded, the purchase always continues. `enforce`: a definitive refusal ends the purchase before any card page; an unknown continues. A buyer who already has an active card goes straight to `quoting` and is not preflighted. |

**Write-back: the live price corrects the catalog** (`REAP_AGENTIC_PRICE_WRITEBACK` = `off` (default) / `shadow` / `on`, services/reap_price_writeback.py). After each poll run, cart-link purchases from the last 72 h that were refused `price_changed` (or continued on a corroborated lower price) are read, newest per product. Without it a buyer told "the price is now X" cannot re-confirm: the gateway and this route still price the product at the old number. Owner rules (2026-10-06): an **increase** is written on the quote alone (the cart URL is ours and names one variant); a **decrease** only when our own store read of the variant says the same price; an **enrichment** listing's offers only when our fresh proof already says it, in either direction (the route requires offers = proof, and the proof is never written from a quote). Mirror rows write the seller's offers on the variant's skus (+ the `::canonical` placeholder under a sole-variant proof) and the seed's own variant price (+ `price_amount` when the seed lists only that variant); enrichment rows write the listing's offers on the proof's skus. Every write is compare-and-set on the old price, a seed crawled after the quote is left alone (`catalog_read_newer`), and the result is checked with this route's own loader (`written` vs `written_not_effective`). `shadow` decides and logs (`would_write`) and writes nothing. Outcomes are logged as `reap_price_writeback: mode=... outcomes={...}` and never enter `PollReport`. The gateway caches mirror product detail up to 10 min, so `get_product` can lag a write by that long.

**What corroborates.** Only cart-link purchases (the variant lane's opaque Reap handles name no
storefront variant; its own resolver price check, `_price_verdict`, is unchanged). The read must
name the numeric variant in the purchase's own cart URL:

* `enrichment_cart_variant_proofs` rows for (product, that variant): outcome `ok`, a known source,
  available, the same storefront host, `currency` equal to the purchase's, a positive price, and
  `checked_at` not in the future and inside the window. All usable rows must agree.
* the mirror seed's storefront proof (`snapshot.shopify_cart_proof` /
  `shopify_cart_variant_proofs`). **It corroborates only if the proof itself records a `currency`
  equal to the purchase's — and no writer records one today, so today it never does.**
  `products.js` carries no currency (its price is in whatever presentment currency the storefront
  chose for our crawler, ×100 even for zero-decimal currencies); the seed's price currency and the
  market currency describe other numbers. An assumed currency is how a substituted variant's
  price would slip through, so it is not assumed.

With either witness dial armed, a quote that fails the subtotal AND another check now reports the other check's code (`quote_total_not_reconciled`, `quote_shipping_not_reconciled`, ...) — still `price_changed` / `price_unverifiable`, never a continue. Both lanes yielding different prices is not corroboration. The subtotal must be an exact multiple
of the quantity. `our_price_minor` is **never rewritten**: it stays the price the buyer selected
(and the money pair bound into the attempt's fingerprint). After a lower rebind the charge is
Reap's own quote total (`quoted_total_minor`, `finalAmount`), the pilot `max_total_minor` cap is
still enforced on that total, and `final_total_minor` / attribution read the charge as before.

**Preflight outcomes** (`preflight_outcome`, recorded once per attempt; a second tick never quotes
again): `ok`; `price_changed` (subtotal, currency, or a corroborated increase — definitive);
`refused` (definitive: `VARIANT_UNAVAILABLE`, `QUOTE_UNFULFILLABLE`, `CHECKOUT_URL_INVALID`,
`CARD_PAYMENT_UNAVAILABLE`, no shipping option on the cart-link lane, or an `items` echo that is
not our line — Reap priced another item, `quote_items_mismatch` → `refusal_reason`
`variant_unavailable` / `quote_unfulfillable` / `cart_link_rejected` / `card_payment_unavailable`
/ `no_shipping_option` / `price_unverifiable`); `unverified` (transport error, timeout, 429, 5xx, an offer-code refusal,
an unreadable quote, or a witness interrupted mid-call: `preflight_interrupted`) — `enforce`
continues on `unverified`, and the approval quote still decides. An enforced refusal is the
ordinary terminal `refused` (email, address and offer code are cleared).

**New GET keys** (owner GET, list and replay; built from `PUBLIC_PURCHASE_COLUMNS`, each present
**only when it applies**, otherwise absent):

```json
"preflight": {"checked_at": "2026-10-05T08:00:01.123456+00:00",
              "totals": {"currency": "USD", "items_subtotal_minor": 3000, "shipping_minor": 500,
                         "tax_minor": 0, "tax_included": false, "total_minor": 3500}},
"live_price": {"currency": "USD", "unit_price_minor": 3000, "items_subtotal_minor": 3000,
               "quoted_total_minor": 3500, "stage": "preflight"},
"price_rebound": {"currency": "USD", "from_unit_price_minor": 3200, "to_unit_price_minor": 3000,
                  "source": "enrichment_proof", "corroborated_at": "2026-10-05T08:00:01.123456+00:00"}
```

* `preflight` — the buy-intent quote confirmed the price (`ok`), present ONLY while no approval
  quote exists (`state` `resolving` or `needs_enrollment`). These are the totals the witness saw
  before the card page, not a promise: from `quoting` on the key is gone and `totals` (the
  approval quote's `quoted_total_minor`) is the only number to show; the approval quote may
  differ. `tax_included: true` means `tax_minor` is already inside the prices.
* `live_price` — only on `state: "refused"` with `refusal_reason: "price_changed"` AND
  `last_error_code` `quote_items_subtotal_mismatch` or `quote_price_increased_corroborated` (the
  refusal is the quoted item price itself), when that quote's live price was recorded: tell the
  buyer "price updated to X". Every `quoting` step discards the witness's live price and rebind
  first, so a later refusal never carries an earlier quote's price. `unit_price_minor` is `null` when
  the subtotal is not an exact multiple of the quantity; `stage` is `preflight` or `approval`.
  **Do not auto-retry at the live price.** `POST /purchases` compares the expected pair with the
  CATALOG offer price (`our_price_minor`, read fresh at create), so a new attempt carrying the live
  price is refused `409 price_changed` until the catalog offer itself is corrected (on the
  enrichment lane the offer must also equal the storefront proof, else `row_price_stale`), and a
  new attempt at the old catalog price meets the same quote refusal. Show the buyer the live
  price; a retry is only meaningful after the catalog has caught up, with a fresh key.
* `price_rebound` — the purchase continued at a lower live unit price our own read corroborated
  (`source`: `enrichment_proof` | `mirror_proof`). `totals.our_price_minor` still shows the
  selected price; the buyer approves `totals.quoted_total_minor`.

`refusal_reason` / `last_error_code` added: `preflight_refused` (fallback reason, not expected),
`quote_price_increased_corroborated`, `preflight_interrupted` (recorded, never terminal).

### States the door will see

`resolving` → `needs_enrollment` (buyer must enrol a card) → `quoting` → `awaiting_approval`
(buyer must approve) → `processing` → `completed`. Also `refused`, `failed`, `expired`.
`resolving` may go straight to `quoting` when the buyer is already enrolled. The buyer needs a
link in exactly the two states named above.
In `resolving`, `needs_enrollment` or `quoting` the buyer may instead be needed to re-enter their
contact: `contact_reentry_required: true` (see "Resume a contact-paused purchase" below).

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
      "consent_version": "reap-agentic-v1",
      "consented_at": "2026-09-18T07:15:49.926588+00:00",
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
      "poll_after_seconds": null,
      "checkout_dispatch_state": "dispatched",
      "contact_reentry_required": false
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

### A repointed buyer re-enrols once — and the old identity is retired for them (WP4c)

If the same human **later** signs in through the hosted checkout, `POST /buyer/save_from_checkout`
repoints the link to their real account — correctly, a verified account supersedes a placeholder.
Because `reap_agentic_buyer_refs` is keyed on the buyer id, their next purchase mints a fresh ref
and **Reap asks for the card once more**. *That is the whole of what the door should expect:* one
extra card entry, at most once per buyer. It is not a bug report.

**What used to be left behind is now retired at the moment of the repoint.** WP4c put a hook on
`routes/buyer_api._upsert_buyer_identity_link` — the surface that does the repointing — which
calls `db.reap_agentic_ledger.retire_buyer_refs_for_buyer(old_buyer_id,
reason="buyer_link_repointed")`:

| what the repoint strands | what happens to it now |
|---|---|
| the old `reap_agentic_enrollments` row(s) | every non-dead one is marked `status = 'dead'` with `reap_status = 'buyer_link_repointed'`, and its `hosted_url` is cleared — so the live card-entry page stops being a live card-entry page |
| the old `reap_agentic_buyer_refs` row | **deleted.** A consent tag on a buyer id that no link names is a record nobody can find, and leaving it holds a `reap_buyer_ref` under `uq_reap_agentic_buyer_refs_ref` for an identity that will never transact again. The live account records a fresh consent on its own row at its next purchase |
| the buyer's **purchases** | untouched, **including their consent evidence.** A purchase is owned by `(agent_id, agent_user_ref_hash)` on its own row, which the repoint does not change, so history stays readable by the agent that made it — and since migration **233** each row carries the `consent_version` / `consented_at` that was in force when *that* purchase was opened |
| the enrollment **at Reap** | still ours to revoke separately. `services.reap_agentic_client.revoke_enrollment` is the call (`POST /agentic/enrollments/{id}/revoke`, in the pinned spec) and it is **not** made from the checkout path — a partner POST there can take up to 25 s with a human waiting. See the runbook |

**What the delete does not cost, since migration 233:** the consent evidence. It used to — this
page said so, in these words: "`reap_agentic_purchases` has no consent column, so the
`consent_version` the *retired* identity accepted is not retained anywhere after the sweep."
Migration 233 gave the purchase row its own `consent_version` / `consented_at`, immutable after the
`INSERT`, which the sweep does not touch. **The refs row's tag is only the LATEST consent**, kept
so the next purchase can re-use it and so the cart-link lane can check a minted identity against
it; the per-purchase copy is the evidence. The identity being retired is one nothing can reach;
the account the buyer actually uses always has a current consent row.

**Two cases the hook deliberately does not fire on.** A repoint away from a buyer who **still has
other links** (a real account linked through several agents, losing one of them) is left alone and
logged `event=reap_buyer_link_repointed_kept` — `reap_agentic_buyer_refs` is keyed on the buyer id,
not on `(agent, ref)`, so retiring there would kill a card the buyer is actively using through
another agent. And a first insert, an idempotent re-upsert, or an upsert that failed are not
repoints at all.

The hook cannot fail or slow the checkout: it is wrapped, it never re-raises, it makes no partner
call, and it logs two integers and no identifiers.

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
* **Latest wins *on the buyer row*.** `reap_agentic_buyer_refs.consent_version` is rewritten on
  **every** `POST` that succeeds, alongside a `consented_at` timestamp — **including an idempotent
  replay**. Send a newer tag with a retried `idempotency_key` and the stored tag moves, even
  though the response is the original purchase.
* **The purchase row keeps the tag it was opened under, for ever.** Migration **233** stores the
  same validated string on `reap_agentic_purchases` in the same request, and nothing rewrites it:
  it is absent from the transition statement, so no poller step can revise it, and the terminal
  write that `NULL`s the buyer's email and shipping address leaves it alone. A completed purchase
  keeps its consent and none of the buyer's PII. It comes back on `GET /purchases/{id}` and on the
  list, as `consent_version` and `consented_at` — a buyer's own consent tag is theirs to read. It
  is `null` only on rows opened before 233.
* **So an idempotent replay with a NEW tag leaves the two disagreeing, and that is correct.** The
  buyer row moves to the new version; the replayed purchase still names the version it was
  actually opened under, because that is what it is evidence of.
* **It is deliberately NOT part of the idempotency request hash.** That hash covers what *decides
  the purchase* — merchant, product, variant, quantity, buyer email, shipping address, return
  url. Consent is not one of those: folding it in would turn a door that upgraded its consent
  version mid-retry into `409 idempotency_conflict`, which is the opposite of what you want from
  a client that just collected a *stronger* consent. So a retry carrying a new tag replays to the
  same purchase **and** records the new tag.
* **A non-string is `invalid_request`, not `consent_required`.** `123`, `true`, `{}` and `[]` are
  refused as a malformed body; they never reach the consent check. `consent_required` means "go
  and ask your user", `invalid_request` means "fix your JSON".

The dial is still checked **first**: a dark rail answers `404` to a request with no consent, the
same as to every other request, so this field cannot be used to probe whether the rail is armed.

---

## Latency and polling

`POST` returns without a partner call, so it is fast. Everything after it is the poller's, on a
30-second cadence by default; one `quoting` step can take up to ~170 s. The door should poll
`GET` at `poll_after_seconds` and must not assume a hosted URL exists on the first read after
the POST — `resolving` has no page yet.

There are **no webhooks on this rail**. The poll is the only way an outcome is ever learned.


### Recover a lost create response

`POST /agent/v2/commerce/reap/purchases/recover` takes the original `StartPurchaseRequest` body and a required nonempty `idempotency_key`. It uses the same API-key and end-user authentication and exact owner pair as create/GET. It is available while create flags or provider credentials are disabled. It does not check current merchant/catalog eligibility, call Reap, create identities/purchases, write consent, refresh keys or retain new buyer PII.

The canonical hash is identical to create: merchant domain, product/variant, quantity, normalized buyer email/address, resolved return URL, item source and optional offer code. Keep the exact original body and resolved return URL; a changed configured default after an omitted return URL safely causes a conflict. Consent version is validated but is not hashed or rewritten by recovery. Click context is not hashed.

* `200`: normally the same redacted owner purchase view as GET-by-ID, including `checkout_dispatch_state` and `contact_reentry_required`. **Except** for an attempt an operator retired before it opened a purchase: then the body is only `{"recovery_status": "retired", "reconciliation_id": "<id>"}`, with no `checkout_dispatch_state`, no `contact_reentry_required` and no purchase fields. Branch on `recovery_status` first. A retired attempt opened no purchase and cannot continue; a new purchase needs new buyer intent and a fresh key.
* `404 purchase_not_found`: unknown key, refusal tombstone, missing purchase or unowned purchase.
* `409 idempotency_conflict`: a changed request or unverifiable stored fingerprint.
* `400`: malformed original body or missing/invalid key; `401`: missing end-user identity.
* `503 checkout_outcome_unknown`: the retirement receipt could not be read, or the key's stored mapping is a refusal marker this server cannot interpret. Retry recovery with the same body and key; never re-POST.
* `500`: any other database error (for example while reading the key mapping or the purchase). It says nothing about the outcome; retry recovery the same way, never re-POST.

A failed recovery preserves uncertainty; it never authorizes a new payment attempt. Retry read-only recovery or escalate with the original key. On the enabled create route, a same-body replay still returns `202` for the existing purchase and follows the established consent update contract. Disabled create remains disabled; use recover for a read-only lookup. No schema migration is needed. Do not delete or overwrite the key mapping merely because 24 hours elapsed. Older application versions can still perform rollover, so replace all create handlers before relying on the lifetime guarantee.




### Resume a contact-paused purchase — `POST /agent/v2/commerce/reap/purchases/{purchase_id}/resume`

The one way to continue a purchase whose `contact_reentry_required` is `true`. It puts the buyer's
contact back on **the same purchase** — same id, buyer reference, click, cart URL, enrollment,
consent and key — and makes it due for the poller now. It never opens a purchase, mints a key,
calls Reap or changes the item, quantity or price. There is no create fallback.

**When to call it.** Only when a `GET` (or list, recover, or create replay) shows
`contact_reentry_required: true` **and** `checkout_dispatch_state: "not_dispatched"`. Ask your
user to continue, then send the request below. Any other dispatch state is refused, because a
checkout may already exist: keep polling, and never open a replacement.

**Gates and authentication.** Same headers as create. The base rail, credentials, the create gate
and (for a `cart_link` body) the cart-link gate must all be on, and the pilot scope must admit the
purchase; otherwise `404 not_available_on_this_rail`, exactly like create. The rail and the create
gate are checked first; the cart-link gate and the pilot scope are checked late, as part of the
fresh admission, so a purchase can get a `409` (for example `terminal_purchase_not_resumable`)
before their `404`. **While create is paused, resume is unavailable** and the re-entry window
keeps running.

**Request.** The **original create body, unchanged**: the same JSON that opened the purchase,
including the same `idempotency_key`, the same buyer email and shipping address (the server
erased them and cannot fill them in), the same `return_url` (or the same omission), the same
`offer_code` (or the same omission), the same `item_source`, and the same
`expected_unit_price_minor` / `expected_currency` pair. It is checked with create's canonical
request hash, so a different body is `409 idempotency_conflict`; changing an address or email is
outside this endpoint's authority. Three further conditions, each checked against the stored
purchase:

* the key must belong to **the same agent and the same buyer** (`X-Agent-User-JWT`) and map to
  **this** `purchase_id`; anything else is `404 purchase_not_found`, the same answer as a
  purchase that does not exist;
* `buyer.consent_version` must equal the tag the purchase was opened under (`consent_version` on
  `GET`), else `400 consent_required`, and the buyer identity link must still name the purchase's
  buyer reference, else `409 buyer_unlinked`;
* a **fresh admission** of the same selection: purchasability and eligibility of the merchant in
  the buyer's market, the catalog row and variant (cart-link: the current Tier B verdict and
  storefront proof), and the pilot scope, exactly as create checks them. The selection must
  resolve to the **same** product, variant, merchant, market, quantity and item source
  (`409 resume_selection_changed`) at the **same** unit price and currency
  (`409 price_changed`).

**Response — `200 OK`.** The same owner view as `GET /purchases/{purchase_id}`, now with
`contact_reentry_required: false`. A repeat of an accepted re-entry (same body) answers `200` with
the current view and does **not** restart the contact clock. Concurrent re-entries have one
winner; a loser that finds the winner's re-entry answers `200` with the same view. An accepted re-entry restarts the contact-retention cap
(`REAP_AGENTIC_CONTACT_MAX_AGE_SECONDS`), so a buyer who leaves it again can be paused again and
resume again. A repeat after the purchase has moved on is answered by the first check below that
fails (for example `checkout_dispatch_unresolved` once a checkout create has started). The table
lists the refusals **in the order the handler checks them**; the first that applies is the answer.

| status | `detail.error` | meaning | what the door should do |
|---|---|---|---|
| 404 | `not_available_on_this_rail` | rail dark or unconfigured, or the create gate off | show unavailable; keep polling `GET`. The re-entry window is still running |
| 401 | `agent_user_required` | no `X-Agent-User-JWT` | obtain the original buyer session |
| 400 | `invalid_request` | the body is not JSON or does not validate as a create body, or an identifier is malformed | send the exact original body |
| 400 | `consent_required` | `buyer.consent_version` is unusable | send the original tag |
| 400 | `invalid_offer_code`, `invalid_address` | the re-entered contact does not validate | send the exact original body |
| 409 | `idempotency_conflict` | the key was used for a **different** body (a changed email, address, price pair, return url, offer code, ...) | send the exact original body |
| 409 | `merchant_not_eligible`, `attempt_retired` | the key names a remembered refusal or a retired attempt, not a purchase | do not create; this attempt cannot continue |
| 503 | `checkout_outcome_unknown` | the key's stored mapping is a refusal marker this server cannot interpret (a database error reading it is a `500`) | call `POST /purchases/recover` with the same body and key, never re-POST |
| 404 | `purchase_not_found` | the key is unknown for this agent and buyer, maps to another purchase, or the purchase is not theirs | do not create; check you hold the original key and buyer session |
| 409 | `terminal_purchase_not_resumable` | the purchase is `completed`, `failed`, `refused` or `expired` (including `contact_reentry_lapsed`) | show the outcome; a new purchase needs new buyer intent and a fresh key |
| 409 | `checkout_dispatch_unresolved` | `checkout_dispatch_state` is not `not_dispatched`: a checkout create started, a checkout exists, or the purchase predates tracking | keep polling `GET`; never re-create |
| 409 | `contact_reentry_not_required` | the contact was never erased (or the purchase is not in `resolving` / `needs_enrollment` / `quoting`). An already accepted re-entry answers `200` here instead | keep polling `GET` |
| 400 | `consent_required` | `buyer.consent_version` **differs from the one the purchase was opened under** | send the original tag |
| 409 | `buyer_unlinked` | the buyer's identity link no longer names this purchase's buyer reference (for example it was repointed by a hosted sign-in) | do not create; escalate |
| 409 | `merchant_not_purchasable` | fresh admission: the merchant is not purchasable in the buyer's market | show blocked; do not switch routes |
| 404 | `not_available_on_this_rail` | fresh admission, `cart_link` body only: the cart-link lane is off | show unavailable; keep polling `GET` |
| 409 | `merchant_disabled`, `merchant_not_eligible`, `row_*`, `seller_identity_unverified` | fresh admission refused the same selection (merchant, eligibility, catalog row or variant, cart-link storefront proof) | show blocked; do not switch routes |
| 409 | `resume_selection_changed` | the catalog now resolves to a different selection than the purchase holds | show blocked; do not switch routes |
| 409 | `price_changed` | the catalog unit price or currency differs from the purchase's | show the change; never resume or retry at another price |
| 404 | `not_available_on_this_rail` | fresh admission, last: the pilot scope is invalid or refuses this purchase | show unavailable; keep polling `GET` |
| 409 | `resume_raced` | the single conditional write lost: a worker holds the purchase, or its state, dispatch fence or contact revision changed in between, and no concurrent re-entry succeeded | wait `poll_after_seconds`, `GET`, and resume again only if it still says so |

**If nobody resumes.** The purchase stays paused and visible to `GET`; nothing is quoted or
dispatched. The re-entry window (`REAP_AGENTIC_CONTACT_REENTRY_WINDOW_SECONDS`, default
86400 = 24 h, settable 3600–604800) is **measured from `contact_purged_at`**, the moment the
contact was erased, not from creation. Once it has passed, the poller ends the purchase with
`last_error_code: "contact_reentry_lapsed"` (`needs_enrollment` → `expired`, `resolving` →
`failed`, `quoting` → `failed`), and `/resume` then answers `409 terminal_purchase_not_resumable`.
It never ends a purchase that has any dispatch evidence (a checkout create started and not
proven not-created, a stored checkout or order, an observed checkout, or a `quoting` purchase
that predates dispatch tracking), nor one a worker holds at that moment: those stay paused for
the operator queue. An accepted resume clears `contact_purged_at`; if the buyer leaves again and
the contact is erased again, a new window starts from that erasure. A lost `/resume` response is
recovered by `GET`, not by a new purchase.




### Optional direct-create pilot controls

`REAP_AGENTIC_CREATE_ENABLED` pauses fresh work independently of status and authenticated recovery. Production requires `REAP_AGENTIC_PILOT_SCOPE` with all five cohort lists plus `variant_keys`, `currency` and `max_total_minor`; the exact literal `unrestricted` is the only explicit opt-out. Missing, malformed, incomplete or outside-scope create admission returns the private `404 not_available_on_this_rail` before buyer identity/consent writes. Quantity is a strict integer for create; authenticated recovery retains the earlier accepted numeric-body normalization. Existing owner GET/recover and checkout reconciliation remain independent of current create scope. See the runbook for variant namespaces, quote caps and worker pause diagnostics.

### Immutable selected money on new attempts

Every NEW attempt carries `expected_unit_price_minor` and `expected_currency`
together (required since 2026-10-04; a body without them is `invalid_request`
before any write, after one read-only replay lookup). A caller using a prepared selection sends the prepared money. The minor amount is a
positive strict integer no larger than 9007199254740991; the currency is exactly
three uppercase letters. A partial pair, explicit null, boolean, float or numeric
string is `invalid_request` (400). These fields constrain the selection; they
never override the server's own offer price.

Both lanes compare the pair with the freshly resolved authoritative SKU/own offer
before creating a buyer reference, consent record, click, key or purchase. A
changed unit amount or currency is `price_changed` (409); existing source and
market-currency refusals also remain in force. A bound variant request refused
before money admission does not create an eligibility tombstone (so no create
writes one any more; a tombstone written earlier is still honoured). Once accepted,
the purchase stores that authoritative unit amount/currency. The existing
provider resolution and exact quote subtotal/currency checks continue to compare
against that stored purchase, including shipping/tax total pilot limits.

The supplied pair is included in the immutable request fingerprint. A same-key
retry or authenticated recovery compares the original pair and returns the
original purchase even if today's catalog money, eligibility or proof changed.
Adding, removing or changing either field on an existing key is an
`idempotency_conflict` (409). Recovery performs no current selection lookup.

A body with both fields omitted is hashed with the prior fingerprint
byte-for-byte. On **create** it only replays: if its key names an attempt keyed
that way, the answer is that attempt's (`202` with the same purchase, its
remembered refusal, `attempt_retired`, or `idempotency_conflict`), and nothing is
written -- not even the consent tag a money-bearing replay rewrites. If the key
names nothing, it is `400 invalid_request` and nothing is written. **Recovery**
accepts it as before. Old client attempts—including attempts whose client retained a selection witness
without putting a money pair on the original backend body—must recover with that
original body. Do not infer or add money fields from a retained witness, do not
remint the attempt, and do not switch checkout routes after any refusal.
